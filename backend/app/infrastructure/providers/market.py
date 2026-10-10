"""行情数据源适配：只解析第三方 HTTP 数据，不持有业务缓存或决定组合估值。"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.common.units import monetary_calculation
from app.core.resources import BusinessResources, resolve_resources
from app.infrastructure.blockchain.chains import ChainMeta
from app.modules.market.schemas import MarketSource


@dataclass(slots=True)
class RawQuote:
    """一个 token 的原始报价。

    字段多半是可选的，因为两个源给的字段集不同：DefiLlama 只能填 price_usd，
    其余留空 —— 留空会在契约里表现为 null，界面上显示为「—」，
    而不是被伪造成 0。
    """

    price_usd: Decimal
    source: MarketSource
    change_24h: float | None = None
    market_cap: Decimal | None = None
    volume_24h: Decimal | None = None
    updated_at: datetime | None = None


@monetary_calculation
async def fetch_dexscreener(
    client: httpx.AsyncClient,
    chain: ChainMeta,
    addresses: tuple[str, ...],
    *,
    resources: BusinessResources | None = None,
) -> dict[str, RawQuote]:
    """DexScreener：唯一能一次给全四个字段的免费源。

    用按链端点 `/tokens/v1/{chainId}/{addresses}`（一次最多 30 个地址）。它只返回
    该链的池子，比拿旧版 `/latest/dex/tokens/` 再自己按 chainId 过滤少一层出错机会。
    """
    base_url = resolve_resources(resources).settings.dexscreener_base_url
    url = f"{base_url}/tokens/v1/{chain.dexscreener_id}/{','.join(addresses)}"
    response = await client.get(url)
    response.raise_for_status()
    payload = response.json()
    pairs = payload if isinstance(payload, list) else []

    wanted = {address.lower() for address in addresses}
    # 同一个 token 往往有几十个池子，取流动性最大的那个报价：池子越浅，
    # 价格越容易被单笔交易带偏。
    # 直接报价和反算报价分开比较；反算时不沿用 base token 的涨跌、市值等指标。
    direct: dict[str, tuple[Decimal, RawQuote]] = {}
    inverse: dict[str, tuple[Decimal, RawQuote]] = {}

    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        price_usd = _decimal(pair.get("priceUsd"))
        if price_usd is None or price_usd <= 0:
            continue
        liquidity = _decimal((pair.get("liquidity") or {}).get("usd")) or Decimal(0)

        base = _node_address(pair.get("baseToken"))
        if base in wanted:
            _keep_best(direct, base, liquidity, price_usd, pair)

        quote = _node_address(pair.get("quoteToken"))
        if quote in wanted:
            # 目标只作为计价方出现时，价格得反算：priceUsd 是「1 个 base 值多少
            # 美元」，priceNative 是「1 个 base 值多少个 quote」，相除即
            # 「1 个 quote 值多少美元」。
            native = _decimal(pair.get("priceNative"))
            if native:
                _keep_best(inverse, quote, liquidity, price_usd / native, {})

    # 同一个 token 同时作为 base 和 quote 出现在不同池子里时，以 base 为准：
    # 反算多绕一次除法，能不用就不用。
    picked: dict[str, RawQuote] = {}
    for address in wanted:
        best = direct.get(address) or inverse.get(address)
        if best is not None:
            picked[address] = best[1]
    return picked


async def fetch_defillama(
    client: httpx.AsyncClient,
    chain: ChainMeta,
    addresses: tuple[str, ...],
    *,
    resources: BusinessResources | None = None,
) -> dict[str, RawQuote]:
    """DefiLlama：只有价格，但 BGT 这类无 DEX 池的币只有它给得出价。"""
    coins = ",".join(f"{chain.defillama_id}:{address}" for address in addresses)
    response = await client.get(
        f"{resolve_resources(resources).settings.defillama_base_url}/prices/current/{coins}"
    )
    response.raise_for_status()
    payload = response.json().get("coins") or {}

    out: dict[str, RawQuote] = {}
    for key, item in payload.items():
        if not isinstance(item, dict):
            continue
        price = _decimal(item.get("price"))
        if price is None or price <= 0:
            continue
        prefix, sep, address = str(key).partition(":")
        # 顺带校验链前缀。它理论上不会错，但校验成本近乎为零，
        # 而"读到别的链的价格"是那种数字看着合理、极难发现的错误。
        if not sep or prefix != chain.defillama_id:
            continue
        out[address.lower()] = RawQuote(
            price_usd=price,
            source="defillama",
            updated_at=_timestamp(item.get("timestamp")),
        )
    return out


def _keep_best(
    bucket: dict[str, tuple[Decimal, RawQuote]],
    address: str,
    liquidity: Decimal,
    price_usd: Decimal,
    pair: dict[str, Any],
) -> None:
    """为同一地址保留流动性更高的池子，比较与报价同时保存，避免字段来自不同池。"""
    current = bucket.get(address)
    if current is None or liquidity > current[0]:
        bucket[address] = (liquidity, _quote_from_pair(pair, price_usd))


def _quote_from_pair(pair: dict[str, Any], price_usd: Decimal) -> RawQuote:
    """把选中的 DexScreener 池转换为统一报价；缺失的附加指标保留 None。"""
    change = pair.get("priceChange") or {}
    market_cap = _decimal(pair.get("marketCap"))
    return RawQuote(
        price_usd=price_usd,
        source="dexscreener",
        change_24h=_float_or_none(change.get("h24") if isinstance(change, dict) else None),
        market_cap=market_cap,
        volume_24h=_decimal((pair.get("volume") or {}).get("h24")),
    )


def _node_address(node: object) -> str:
    """从 baseToken / quoteToken 节点里取小写地址；形状不对就返回空串。"""
    if not isinstance(node, dict):
        return ""
    address = node.get("address")
    return address.lower() if isinstance(address, str) else ""


def _decimal(value: object) -> Decimal | None:
    """行情源给的数字或字符串 → Decimal；取不到返回 None。

    不用 float 中转：`priceUsd` 本身是字符串，`Decimal(str(value))` 能原样保留
    它给的位数，走 float 会在第 15 位有效数字上失真。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() and parsed >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    """解析可选比率字段，拒绝布尔值、非数值及无穷值，保留合法负涨跌幅。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: object) -> datetime | None:
    """将上游 Unix 秒转成 UTC 时间；非法类型和越界值视为没有可靠时间戳。"""
    try:
        return datetime.fromtimestamp(int(value), UTC)  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError, OverflowError):
        return None
