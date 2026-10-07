"""Token 行情（功能点 2）。

契约要四个字段：价格 / 24h 涨跌 / 市值 / 24h 成交量。免费源里能一次给全的只有
DexScreener；而它覆盖不到没有交易池的币（实测 BGT 就没有池子，BVT 更是什么都没有）。
所以这里让两个源互补：

    DexScreener   四个字段全，主源
    DefiLlama     只有价格，但收录了 BGT 这类无池币，补位

两者都按「链标识 + 合约地址」查询，**不依赖任何第三方 coin id** —— 清单里加一行
TokenMeta 就自动有行情，与功能点 1 的"链可配置、加币即一行"是同一套口径。
（这正是不用 CoinGecko 的原因：它的接口按 coin id 查，而 id 要手工去它的站点上找，
小币还未必收录，等于把可配置性又交回给外部。）

⚠️ 同一个合约地址可能部署在多条链上（实测 USDT0 在 Mantle / Stable / Berachain
   都有池子，且 Mantle 上的流动性高一个量级），按地址裸查会跨链串味。
   所以两个源都强制带链标识，见 `constants/chains.py` 的 dexscreener_id /
   defillama_id。
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.api.schemas.market import MarketSource, TokenMarket, TokenMarketList
from app.config.constants.chains import ChainMeta, get_chain
from app.config.constants.tokens import TokenMeta, price_address_of, tokens_for
from app.config.settings import settings
from app.infra.logging import get_logger
from app.infra.singleflight import singleflight
from app.services.units import monetary_calculation
from app.shared.errors import UpstreamError

logger = get_logger(__name__)


@dataclass(slots=True)
class _Quote:
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


# chain_id → (过期时刻, {symbol: TokenMarket})
# 用单调时钟计时而不是墙钟：系统时间被校准不该让缓存提前失效或永不过期。
_cache: dict[int, tuple[float, dict[str, TokenMarket]]] = {}

# 缓存击穿（TTL 到期的瞬间，多个并发请求一起发现过期、一起去打上游）在这里不加锁：
# 代价只是多打几次免费接口，而 `asyncio.Lock` 会绑定到首次使用它的事件循环 ——
# 在测试（每个用例一个 loop）或多 loop 场景下会抛 "bound to a different event loop"，
# 是个很隐蔽的坑。真到了需要压低上游请求量的规模，再引入按链的 single-flight。


async def get_quotes(chain_id: int, symbols: list[str] | None = None) -> TokenMarketList:
    """取某条链的行情。symbols 为空/None 表示返回该链候选清单的全部。"""
    chain = get_chain(chain_id)
    wanted, unknown = _select(tokens_for(chain_id), symbols)

    quotes = await _load(chain)

    return TokenMarketList(
        chain_id=chain.chain_id,
        chain_name=chain.name,
        tokens=[quotes[token.symbol] for token in wanted if token.symbol in quotes],
        missing=unknown + [token.symbol for token in wanted if token.symbol not in quotes],
    )


def _select(tokens: tuple[TokenMeta, ...], symbols: list[str] | None) -> tuple[list[TokenMeta], list[str]]:
    """按请求的 symbol 过滤清单，返回（命中的条目, 清单里没有的 symbol）。

    拼错的 symbol 要报成 missing 而不是静默丢弃：否则一个笔误换来的是一份
    "看起来正常、就是少了几个币"的响应，是最难查的那类问题。
    """
    if not symbols:
        return list(tokens), []

    by_symbol = {token.symbol.upper(): token for token in tokens}
    picked: list[TokenMeta] = []
    unknown: list[str] = []
    for raw in symbols:
        name = raw.strip()
        if not name:
            continue
        token = by_symbol.get(name.upper())
        if token is None:
            unknown.append(name)
        elif token not in picked:
            picked.append(token)
    return picked, unknown


def _usable_quotes(quotes: dict[str, TokenMarket]) -> dict[str, TokenMarket]:
    max_age = settings.market_cache_ttl + settings.market_max_stale_seconds
    now = datetime.now(UTC)
    return {
        symbol: quote
        for symbol, quote in quotes.items()
        if 0 <= (now - quote.updated_at).total_seconds() <= max_age
    }


@singleflight
async def _load(chain: ChainMeta) -> dict[str, TokenMarket]:
    """取该链的行情表，带缓存与降级。"""
    cached = _cache.get(chain.chain_id)
    if cached is not None and cached[0] > time.monotonic():
        return _usable_quotes(cached[1])

    try:
        fresh = await _fetch(chain)
    except UpstreamError as exc:
        if cached is None or time.monotonic() - cached[0] > settings.market_max_stale_seconds:
            raise
        # 两个源都挂了：过期的价格也比空表有用，但必须标出来 ——
        # 不标的话用户看到的是旧价却以为是当前价，比看不到更糟。
        logger.warning("行情刷新失败，回退过期缓存：%s", exc)
        return {s: q.model_copy(update={"stale": True}) for s, q in _usable_quotes(cached[1]).items()}

    _cache[chain.chain_id] = (time.monotonic() + settings.market_cache_ttl, fresh)
    return fresh


async def _fetch(chain: ChainMeta) -> dict[str, TokenMarket]:
    """同时问两个源，再按 token 合并。只要有一个源活着就算成功。"""
    targets = _targets(tokens_for(chain.chain_id))
    if not targets:
        return {}

    # 请求用原始大小写（checksum）地址，匹配用小写 —— 两个外部源对大小写的
    # 处理各不相同，进出一律各自归一，避免"大小写不同所以没匹配上"这类静默失配。
    addresses = tuple(original for original, _ in targets.values())

    async with httpx.AsyncClient(
        timeout=settings.market_timeout_seconds,
        proxy=settings.market_proxy or None,
        headers={"accept": "application/json"},
    ) as client:
        results = await asyncio.gather(
            _from_dexscreener(client, chain, addresses),
            _from_defillama(client, chain, addresses),
            return_exceptions=True,
        )

    primary = _per_source_errors_to_empty(results[0], "DexScreener")
    fallback = _per_source_errors_to_empty(results[1], "DefiLlama")

    if not primary and not fallback:
        raise UpstreamError("行情源全部不可用（DexScreener / DefiLlama）")

    merged: dict[str, TokenMarket] = {}
    for key, (_, metas) in targets.items():
        # DexScreener 优先。不仅因为它字段全，更因为它的价格与涨跌/市值/成交量
        # 出自同一个池子、内部自洽；一行的四个字段混用两个源，会出现
        # "价格来自 A、涨跌来自 B"的错配，而界面上的数字看起来全都合理。
        quote = primary.get(key) or fallback.get(key)
        if (
            quote
            and quote.updated_at
            and not 0
            <= (datetime.now(UTC) - quote.updated_at).total_seconds()
            <= (settings.market_cache_ttl + settings.market_max_stale_seconds)
        ):
            quote = None
        if quote is None:
            continue
        for meta in metas:
            merged[meta.symbol] = _to_contract(meta.symbol, quote)
    return merged


def _targets(tokens: tuple[TokenMeta, ...]) -> dict[str, tuple[str, list[TokenMeta]]]:
    """折叠成 `{小写地址: (原始地址, [条目, ...])}`。

    折叠是必要的：原生币借用包装币的地址查行情，于是 BERA 与 WBERA 指向同一个
    地址 —— 一次请求覆盖两个条目，也顺带保证两者的价格永远一致。
    """
    out: dict[str, tuple[str, list[TokenMeta]]] = {}
    for token in tokens:
        address = price_address_of(token)
        if address is None:
            continue
        key = address.lower()
        existing = out.get(key)
        if existing is None:
            out[key] = (address, [token])
        else:
            existing[1].append(token)
    return out


def _per_source_errors_to_empty(result: object, name: str) -> dict[str, _Quote]:
    """把一个源的结果拍平成"失败 = 空表"。

    单个源挂掉不该让整次请求失败 —— 另一个源仍能覆盖大部分 token。
    （`asyncio.gather(return_exceptions=True)` 把异常当结果返回，这里负责收掉它，
    否则一个源的网络抖动会直接变成 502。）
    """
    if isinstance(result, BaseException):
        logger.warning("%s 行情获取失败 (%s)", name, type(result).__name__)
        return {}
    return result  # type: ignore[return-value]


@monetary_calculation
async def _from_dexscreener(
    client: httpx.AsyncClient, chain: ChainMeta, addresses: tuple[str, ...]
) -> dict[str, _Quote]:
    """DexScreener：唯一能一次给全四个字段的免费源。

    用按链端点 `/tokens/v1/{chainId}/{addresses}`（一次最多 30 个地址）。它只返回
    该链的池子，比拿旧版 `/latest/dex/tokens/` 再自己按 chainId 过滤少一层出错机会。
    """
    url = f"{settings.dexscreener_base_url}/tokens/v1/{chain.dexscreener_id}/{','.join(addresses)}"
    response = await client.get(url)
    response.raise_for_status()
    payload = response.json()
    pairs = payload if isinstance(payload, list) else []

    wanted = {address.lower() for address in addresses}
    # 同一个 token 往往有几十个池子，取流动性最大的那个报价：池子越浅，
    # 价格越容易被单笔交易带偏。
    direct: dict[str, tuple[Decimal, _Quote]] = {}
    inverse: dict[str, tuple[Decimal, _Quote]] = {}

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
    picked: dict[str, _Quote] = {}
    for address in wanted:
        best = direct.get(address) or inverse.get(address)
        if best is not None:
            picked[address] = best[1]
    return picked


async def _from_defillama(
    client: httpx.AsyncClient, chain: ChainMeta, addresses: tuple[str, ...]
) -> dict[str, _Quote]:
    """DefiLlama：只有价格，但 BGT 这类无 DEX 池的币只有它给得出价。"""
    coins = ",".join(f"{chain.defillama_id}:{address}" for address in addresses)
    response = await client.get(f"{settings.defillama_base_url}/prices/current/{coins}")
    response.raise_for_status()
    payload = response.json().get("coins") or {}

    out: dict[str, _Quote] = {}
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
        out[address.lower()] = _Quote(
            price_usd=price,
            source="defillama",
            updated_at=_timestamp(item.get("timestamp")),
        )
    return out


def _keep_best(
    bucket: dict[str, tuple[Decimal, _Quote]],
    address: str,
    liquidity: Decimal,
    price_usd: Decimal,
    pair: dict[str, Any],
) -> None:
    current = bucket.get(address)
    if current is None or liquidity > current[0]:
        bucket[address] = (liquidity, _quote_from_pair(pair, price_usd))


def _quote_from_pair(pair: dict[str, Any], price_usd: Decimal) -> _Quote:
    change = pair.get("priceChange") or {}
    market_cap = _decimal(pair.get("marketCap"))
    return _Quote(
        price_usd=price_usd,
        source="dexscreener",
        change_24h=_float_or_none(change.get("h24") if isinstance(change, dict) else None),
        market_cap=market_cap,
        volume_24h=_decimal((pair.get("volume") or {}).get("h24")),
    )


def _to_contract(symbol: str, quote: _Quote) -> TokenMarket:
    return TokenMarket(
        symbol=symbol,
        price_usd=quote.price_usd,
        change_24h=quote.change_24h,
        market_cap=quote.market_cap,
        volume_24h=quote.volume_24h,
        source=quote.source,
        # DexScreener 不回时间戳，退到取数时刻。契约要求必填，而"我们什么时候
        # 拿到的"对判断新鲜度同样有效 —— 留空反而会被误读成"没有更新过"。
        updated_at=quote.updated_at or datetime.now(UTC),
        stale=False,
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
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), UTC)  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError, OverflowError):
        return None
