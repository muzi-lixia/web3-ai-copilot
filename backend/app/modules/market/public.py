"""公开单币行情补充：七日参考报价和法币换算，不读取钱包，不依赖模型。"""

import time
from datetime import UTC, datetime
from decimal import Decimal, localcontext

import httpx

from app.core.exceptions import NotFoundError, UpstreamError
from app.core.logging import span
from app.infrastructure.blockchain.chains import get_chain
from app.modules.asset.tokens import price_address_of, select_tokens
from app.modules.market.service import get_quotes


async def currency_rate(currency, resources):
    """仅支持 USD/CNY。CNY 使用公开汇率源，失败明确返回不可用，不使用固定汇率。"""
    if currency == "USD":
        return Decimal(1), None
    if currency != "CNY":
        raise NotFoundError("仅支持 USD 或 CNY")
    key = "usd_cny"
    cached = resources.apy_cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    with span("market.exchange_rate"):
        try:
            async with httpx.AsyncClient(
                timeout=resources.settings.market_timeout_seconds,
                proxy=resources.settings.market_proxy or None,
                trust_env=False,
            ) as client:
                response = await client.get(
                    "https://api.frankfurter.dev/v1/latest", params={"base": "USD", "symbols": "CNY"}
                )
                response.raise_for_status()
                body = response.json()
                rate = Decimal(str(body["rates"]["CNY"]))
                if not rate.is_finite() or rate <= 0:
                    raise ValueError("invalid rate")
                value = (rate, body["date"])
                resources.apy_cache[key] = (time.monotonic() + 3600, value)
                return value
        except (httpx.HTTPError, KeyError, ValueError):
            raise UpstreamError("人民币汇率暂不可用") from None


async def historical_change(chain, token, current, resources):
    """七日前参考价缺失时返回 null，不从 24h 涨跌推算。"""
    address = price_address_of(token)
    if not address:
        return None
    cache = resources.apy_cache
    key = "history:" + str(chain.chain_id) + ":" + address.lower()
    previous = cache.get(key)
    if previous and previous[0] > time.monotonic():
        old = previous[1]
    else:
        try:
            with span("market.historical", chain_id=chain.chain_id):
                async with httpx.AsyncClient(
                    timeout=resources.settings.market_timeout_seconds,
                    proxy=resources.settings.market_proxy or None,
                    trust_env=False,
                ) as client:
                    ref = chain.defillama_id + ":" + address.lower()
                    response = await client.get(
                        resources.settings.defillama_base_url
                        + f"/prices/historical/{int(time.time()) - 7 * 86400}/{ref}"
                    )
                    response.raise_for_status()
                    old = Decimal(str(response.json()["coins"][ref]["price"]))
                    if not old.is_finite() or old <= 0:
                        return None
                    if len(cache) >= resources.settings.cache_max_entries:
                        cache.pop(next(iter(cache)))
                    cache[key] = (time.monotonic() + 3600, old)
        except (httpx.HTTPError, KeyError, ValueError):
            return None
    with localcontext() as ctx:
        ctx.prec = 100
        return format((current - old) / old * 100, "f")


async def quote(chain_id, symbol, currency, resources):
    token = select_tokens(chain_id, (symbol,))[0]
    chain = get_chain(chain_id)
    quotes = await get_quotes(chain_id, [symbol], resources=resources)
    if not quotes.tokens:
        raise NotFoundError("当前代币暂无可用行情")
    item = quotes.tokens[0]
    rate, rate_date = await currency_rate(currency, resources)
    with localcontext() as ctx:
        ctx.prec = 100
        return dict(
            symbol=item.symbol,
            chain_id=chain_id,
            currency=currency,
            price=format(item.price_usd * rate, "f"),
            market_cap=format(item.market_cap * rate, "f") if item.market_cap is not None else None,
            volume_24h=format(item.volume_24h * rate, "f") if item.volume_24h is not None else None,
            change_24h=item.change_24h,
            change_7d=await historical_change(chain, token, item.price_usd, resources),
            updated_at=item.updated_at.isoformat(),
            source=item.source,
            stale=item.stale,
            exchange_rate_date=rate_date,
            queriedAt=datetime.now(UTC).isoformat(),
        )
