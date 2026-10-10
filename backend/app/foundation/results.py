"""真实资产结果保存在基础服务；模型只得到结果引用和显式元数据投影。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import uuid4

from sqlalchemy import text

from app.core.exceptions import AppError, NotFoundError
from app.core.logging import span
from app.infrastructure.blockchain.chains import CHAINS
from app.modules.asset import service as assets
from app.modules.asset.custom_token import balance as custom_balance


def summary(payload):
    """字段白名单投影，不递归复制资产对象，避免余额或地址流入模型和 Checkpoint。"""
    return {
        "result_id": payload["id"],
        "isComplete": payload["isComplete"],
        "failed_chains": payload["failed_chains"],
        "currency": payload["currency"],
        "symbols": sorted({a["symbol"] for c in payload["chains"] for a in c["assets"]}),
        "unpriced_count": len(payload["unpriced"]),
        "chain_ids": [c["chain_id"] for c in payload["chains"]],
        "queriedAt": payload["queriedAt"],
        "scope": payload["scope"],
    }


class ResultService:
    def __init__(self, database, resources):
        self.db, self.resources = database, resources

    async def save(self, user, payload):
        """提交真实结果后才公开 ID；保存失败不能发送看似成功的数据卡片。"""
        payload = {**payload, "id": str(uuid4())}
        expires = datetime.now(UTC) + timedelta(hours=self.resources.settings.conversation_ttl_hours)
        async with self.db.transaction(user) as s:
            await s.execute(
                text("""INSERT INTO asset_results(id,user_id,payload,expires_at)
                VALUES(:id,:u,CAST(:p AS jsonb),:e)"""),
                dict(id=payload["id"], u=user, p=json.dumps(payload), e=expires),
            )
        return summary(payload)

    async def get(self, user, result_id):
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
        payload = await self.convert(payload, currency)
        return await self.save(identity.user_id, payload)

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
            total_value=format(total, "f"),
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
        payload = await self.convert(payload, currency)
        return await self.save(user, payload)

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
                "total_value": format(Decimal(payload["total_value"]) * rate, "f"),
                "currency": currency,
                "exchange_rate_date": date,
            }

    async def delete(self, user, result_ids):
        async with self.db.transaction(user) as session:
            for rid in set(result_ids):
                await session.execute(
                    text("DELETE FROM asset_results WHERE id=:id AND user_id=:u"), dict(id=rid, u=user)
                )
