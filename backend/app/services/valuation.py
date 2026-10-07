"""把链上余额与行情报价合成 USD 估值。

独立成一个纯函数模块，不碰网络也不碰 RPC：输入是已经读好的余额和报价，
输出是填好单价/估值/占比的资产表与合计。好处是可以直接单测 ——
构造两个 dict 就能覆盖全部边界，不必起节点、也不必 mock HTTP。

三处边界必须写清：

1. **无价 ≠ 无资产。** 取不到行情的持仓，`value_usd` 留 null 而不是按 0 计。
   按 0 计会让总资产虚低，而界面上 `$0.00` 与"不知道值多少"看起来一样，
   指向的处理方式却完全相反。

2. **占比的分母只含"有价的持仓"。** 把无价资产算进分母，所有有价资产的占比
   都会偏小，而那个偏小的比例在数学上没有任何含义。

3. **不在这里裁剪小数位。** 后端给精确十进制、由前端决定显示几位，是全项目的
   约定（见 `schemas/wallet.py` 的序列化说明）。这里一旦 quantize，
   前端就再也拿不回原始值。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.schemas.market import TokenMarket
from app.schemas.wallet import Asset

_HUNDRED = Decimal(100)
_TWO_PLACES = Decimal("0.01")


@dataclass(slots=True, frozen=True)
class Valuation:
    """估值结果。字段与 `WalletAssets` 的对应部分一一对应，由调用方搬运。"""

    assets: list[Asset]
    total_value_usd: Decimal
    has_valuation: bool
    missing_price: list[str]
    stale: bool


def value_assets(assets: list[Asset], quotes: dict[str, TokenMarket]) -> Valuation:
    """给资产表补上单价、估值、占比。

    两遍扫描：第一遍算每条的价值并累计总额，第二遍才有分母算占比。
    一遍做不到 —— 占比依赖总额，而总额依赖每一条。
    """
    entries: list[tuple[Asset, tuple[TokenMarket, Decimal] | None]] = []
    missing: list[str] = []
    total = Decimal(0)
    stale = False
    priced_holdings = 0

    for asset in assets:
        quote = quotes.get(asset.symbol)
        if quote is None:
            entries.append((asset, None))
            # 只报**持仓里**没价的：零余额的币没价不影响任何判断，
            # 全列出来只会让提示栏被噪音塞满。
            if asset.amount != 0:
                missing.append(asset.symbol)
            continue

        value = asset.amount * quote.price_usd
        entries.append((asset, (quote, value)))
        total += value
        stale = stale or quote.stale
        if asset.amount != 0:
            priced_holdings += 1

    valued: list[Asset] = []
    for asset, priced in entries:
        if priced is None:
            valued.append(
                asset.model_copy(update={"price_usd": None, "value_usd": None, "percentage": None})
            )
            continue
        quote, value = priced
        valued.append(
            asset.model_copy(
                update={
                    "price_usd": quote.price_usd,
                    "value_usd": value,
                    "percentage": _share_of(value, total),
                }
            )
        )

    return Valuation(
        assets=valued,
        total_value_usd=total,
        has_valuation=priced_holdings > 0,
        missing_price=missing,
        stale=stale,
    )


def _share_of(value: Decimal, total: Decimal) -> float | None:
    """value 占 total 的百分比（0-100，保留两位）。

    total 为 0 时返回 None 而不是 0：分母不存在，占比没有定义。
    返回 0 会让界面画出"所有资产占比 0%"这种看着像 bug 的图。
    """
    if total <= 0:
        return None
    share = (value / total * _HUNDRED).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    return float(share)
