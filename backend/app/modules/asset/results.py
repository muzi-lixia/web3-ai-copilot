"""真实资产结果保存在基础服务；模型得到白名单资产摘要（含数量、估值），不含钱包地址和凭证。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import uuid4

from sqlalchemy import text

from app.common.units import cut_usd
from app.core.exceptions import AppError, NotFoundError
from app.core.logging import span
from app.infrastructure.blockchain.chains import CHAINS
from app.modules.asset import service as assets
from app.modules.asset.custom_token import balance as custom_balance


def summary(payload):
    """给模型的摘要：带本次结果的真实数值，但只到「值多少」，不含地址与代币合约。

    结果描述改由模型用自然语言生成，所以金额与余额必须让模型看到；
    地址与合约明文仍然剥离 —— 模型需要的是「查到什么、值多少」，不是「在哪里」。
    USD 位数已在序列化出口（`cut_usd`）固定成 6 位，这里直接透传。
    """
    return {
        "result_id": payload["id"],
        "kind": payload.get("kind", "balance"),
        "scope": payload["scope"],
        "currency": payload["currency"],
        "total_value": payload["total_value"],
        "holdings": [
            {
                "chain": chain["chain_name"],
                "symbol": asset["symbol"],
                "amount": asset["amount"],
                "value_usd": asset["value_usd"],
            }
            for chain in payload["chains"]
            for asset in chain["assets"]
        ],
        "unpriced": [asset["symbol"] for asset in payload["unpriced"]],
        "isComplete": payload["isComplete"],
        "failed_chains": payload["failed_chains"],
        "queriedAt": payload["queriedAt"],
    }


def _hhmmss(iso: str) -> str:
    """快照时间转固定 UTC+8 的 HH:MM:SS，供后端组装的来源描述使用。

    不读本机时区：同一份结果在不同机器上必须给出同一个串，否则快照不可复现。
    """
    try:
        return datetime.fromisoformat(iso).astimezone(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
    except ValueError:
        return iso


def envelope(kind, record, *, source_desc):
    """结果的通用外壳：只放跨类型字段，业务数据一律进 body。

    failed 用 {type, ref} 表达，而不是只放链 ID —— 将来失败的可能是一次行情源或一份文档，
    信封不该随「失败的是什么」而改形状。
    """
    return {
        "result_id": record["id"],
        "kind": kind,
        "status": "complete" if record["isComplete"] else "partial",
        "queriedAt": record["queriedAt"],
        "currency": record["currency"],
        "sources": record.get("sources") or [],
        "failed": [{"type": "chain", "ref": cid} for cid in record["failed_chains"]],
        "source_desc": source_desc,
    }


class ResultService:
    """资产查询结果的持久化与汇总，所有读写都按 user_id 归属校验。"""

    def __init__(self, database, resources):
        """持有数据库连接层与业务资源（配置、行情缓存）。"""
        self.db, self.resources = database, resources

    async def save(self, user, payload):
        """提交真实结果后才公开 ID；保存失败不能发送看似成功的数据卡片。

        返回落库后的完整记录（含 id）：调用方据此再投影出「给模型的摘要」与「给前端的结果」。
        """
        record = {**payload, "id": str(uuid4())}
        expires = datetime.now(UTC) + timedelta(hours=self.resources.settings.asset_result_ttl_hours)
        async with self.db.transaction(user) as s:
            await s.execute(
                text("""INSERT INTO asset_results(id,user_id,payload,expires_at)
                VALUES(:id,:u,CAST(:p AS jsonb),:e)"""),
                dict(id=record["id"], u=user, p=json.dumps(record), e=expires),
            )
        return record

    @staticmethod
    def result(record):
        """把落库记录投影成下发给前端的结果：信封 + body。

        body 是资产查询的业务数据（链、明细、合计、未计价），信封只承担跨类型字段；
        **新增 kind 要动两处**：这里加一个 body 分支，前端 `ResultCard` 也要补一支分发 ——
        漏了会落进兜底分支、把原始 JSON 摊在卡片上（aggregate 就这么出现过一次）。
        """
        kind = record.get("kind", "balance")
        chains = record["chains"]
        desc = f"{len(chains)} 条网络 balanceOf · 快照 {_hhmmss(record['queriedAt'])}"
        if kind == "aggregate":
            desc += f" · 合并 {len(record.get('sources') or [])} 份结果"
        body = {
            "scope": record["scope"],
            "chains": chains,
            "total_value": record["total_value"],
            "unpriced": record["unpriced"],
        }
        # 非 USD 计价才附带原始 USD 合计与汇率日期，避免 USD 查询下出现同义重复字段。
        for extra in ("total_value_usd", "exchange_rate_date"):
            if extra in record:
                body[extra] = record[extra]
        return {**envelope(kind, record, source_desc=desc), "body": body}

    async def read(self, user, result_id):
        """读本人结果并投影成信封 + body，供展示接口直接下发（不再让前端二次拼装）。"""
        return self.result(await self.get(user, result_id))

    async def get(self, user, result_id):
        """读本人结果；不存在、已过期或非本人一律 404，不泄露归属信息。"""
        async with self.db.transaction(user) as s:
            row = await s.scalar(
                text("""SELECT payload FROM asset_results
                WHERE id=:id AND user_id=:u AND expires_at>now()"""),
                dict(id=result_id, u=user),
            )
        if row is None:
            raise NotFoundError("查询结果不存在或已过期")
        return row

    async def query(self, identity, chain_id=None, symbol=None, currency="USD"):
        """不同链并行查询，单链失败不丢弃其他链结果；数值仅存业务结果。"""
        ids = [chain_id] if chain_id else [c.chain_id for c in CHAINS]

        async def read(cid):
            """查询单条链的资产；代币合约走清单外读取，其余走注册表批量查询。"""
            with span("rpc.balance", chain_id=cid):
                if symbol and symbol.startswith("0x"):
                    asset = await custom_balance(cid, symbol, identity.address, self.resources)
                    from app.infrastructure.blockchain.chains import get_chain
                    from app.modules.asset.schemas import WalletAssets

                    chain = get_chain(cid)
                    wallet = WalletAssets(
                        address=identity.address,
                        chain_id=cid,
                        chain_name=chain.name,
                        explorer=chain.explorer,
                        assets=[asset],
                        computed_at=datetime.now(UTC),
                        missing_price=[asset.symbol] if asset.amount else [],
                    )
                    return wallet.model_dump(mode="json", exclude={"address"})
                wallet = await assets.get_wallet_assets(
                    cid, identity.address, (symbol,) if symbol else None, True, resources=self.resources
                )
                # 不向 Agent 返回钱包地址。该结果表示本人查询，身份由凭证固定。
                return wallet.model_dump(mode="json", exclude={"address"})

        responses = await asyncio.gather(*(read(cid) for cid in ids), return_exceptions=True)
        chains, failed = [], []
        for cid, response in zip(ids, responses, strict=True):
            if isinstance(response, BaseException):
                if symbol and isinstance(response, AppError) and response.status_code < 500:
                    raise response
                failed.append(cid)
            else:
                chains.append(response)
        if not chains:
            from app.core.exceptions import UpstreamError

            raise UpstreamError("所有查询网络均不可用，请稍后重试")
        payload = self.compose(chains, failed)
        payload["scope"] = "specified_token" if symbol else "registered_tokens"
        payload["kind"] = "balance"
        payload = await self.convert(payload, currency)
        record = await self.save(identity.user_id, payload)
        # 一次查询同时产出两份投影：给模型的数值摘要（含金额，不含地址与合约）
        # 与给前端的结果（含业务数据与来源描述）。
        return {"summary": summary(record), "result": self.result(record)}

    @staticmethod
    def compose(chains, failed, *, sources=None):
        """Decimal 高精度求和；缺价/未知余额/失效价格与零余额分开表达。"""
        total, unpriced = Decimal(0), []
        with localcontext() as ctx:
            ctx.prec = 100
            for chain in chains:
                for a in chain["assets"]:
                    if a["value_usd"] is not None:
                        total += Decimal(a["value_usd"])
                    elif a["amount"] is None or Decimal(a["amount"]) != 0:
                        unpriced.append(
                            {"chain_id": chain["chain_id"], "symbol": a["symbol"], "contract": a["contract"]}
                        )
        complete = not failed and all(c["status"] == "complete" and not c.get("stale") for c in chains)
        return dict(
            chains=chains,
            currency="USD",
            total_value=cut_usd(total),
            unpriced=unpriced,
            isComplete=complete,
            failed_chains=sorted(set(failed)),
            scope="registered_tokens",
            queriedAt=datetime.now(UTC).isoformat(),
            sources=sources or [],
        )

    async def aggregate(self, user, result_ids, currency="USD"):
        """逐资产选择最新来源，防止重复范围叠加；来源失败保守继承，数值不由模型填写。"""
        records = [await self.get(user, rid) for rid in dict.fromkeys(result_ids)]
        if not records:
            raise NotFoundError("结果缺失或计价单位不一致")
        by_chain = {}
        for record in sorted(records, key=lambda r: r["queriedAt"]):
            for chain in record["chains"]:
                cid = chain["chain_id"]
                if cid not in by_chain:
                    by_chain[cid] = {**chain, "assets": {}}
                target = by_chain[cid]
                target["status"] = "partial" if chain["status"] != "complete" else target["status"]
                for a in chain["assets"]:
                    target["assets"][(a["contract"] or "native").lower()] = a
        chains = [{**c, "assets": list(c["assets"].values())} for c in by_chain.values()]
        payload = self.compose(
            chains,
            [cid for r in records for cid in r["failed_chains"]],
            sources=[{"id": r["id"], "queriedAt": r["queriedAt"]} for r in records],
        )
        if any(not r["isComplete"] for r in records):
            payload["isComplete"] = False
        payload["kind"] = "aggregate"
        payload = await self.convert(payload, currency)
        record = await self.save(user, payload)
        return {"summary": summary(record), "result": self.result(record)}

    async def convert(self, payload, currency):
        """统一内部按 USD 保存各资产估值；展示总额可转 CNY，汇总不重复转换。"""
        if currency == "USD":
            return payload
        from app.modules.market.public import currency_rate

        rate, date = await currency_rate(currency, self.resources)
        with localcontext() as ctx:
            ctx.prec = 100
            return {
                **payload,
                "total_value_usd": payload["total_value"],
                "total_value": cut_usd(Decimal(payload["total_value"]) * rate),
                "currency": currency,
                "exchange_rate_date": date,
            }

    async def delete(self, user, result_ids):
        """批量删除本人结果；去重后逐条按 id + user_id 删除，非本人的删不掉也不报错。"""
        async with self.db.transaction(user) as session:
            for rid in set(result_ids):
                await session.execute(
                    text("DELETE FROM asset_results WHERE id=:id AND user_id=:u"), dict(id=rid, u=user)
                )
