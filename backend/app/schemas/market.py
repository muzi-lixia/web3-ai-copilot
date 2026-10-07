"""Token 行情契约（功能点 2）。"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_serializer

from app.schemas.common import Schema

MarketSource = Literal["dexscreener", "defillama"]
"""行情数据源标识。

定义在这里而不是各写出处，是为了让"当前支持哪些源"只有一个答案：
契约、服务层、前端类型都从这里派生，加源时不会漏改某一处。
"""


class TokenMarket(Schema):
    symbol: str
    price_usd: Decimal
    change_24h: float | None = Field(default=None, description="百分比，如 -2.1 表示 -2.1%")
    market_cap: Decimal | None = None
    volume_24h: Decimal | None = None
    source: MarketSource = Field(
        description="这一行取自哪个源。需要暴露，是因为两个源的字段完整度不同："
        "DefiLlama 只给价格，涨跌/市值/成交量必然为 null —— 界面上要能解释"
        "「这个币为什么只有价格」，而不是让人以为是取数失败"
    )
    updated_at: datetime = Field(description="行情源的更新时间，不是本地读取时间")
    stale: bool = Field(default=False, description="上游全部失败、降级到过期缓存时为 True")

    @field_serializer("price_usd", "market_cap", "volume_24h", when_used="json")
    def _decimal_as_plain_string(self, value: Decimal | None) -> str | None:
        """金额序列化成十进制字符串。

        理由同 `schemas/wallet.py` 的 amount：JSON number 走 JS 的 float64，
        大额会掉精度。`format(value, "f")` 同时避免极小值被写成科学计数法。
        """
        return None if value is None else format(value, "f")


class TokenMarketList(Schema):
    """批量行情（一次请求覆盖多个 token，减少出网次数）。"""

    chain_id: int
    chain_name: str = Field(description="展示用。前端不内置链清单，链名一律由后端给")
    tokens: list[TokenMarket]
    missing: list[str] = Field(
        default_factory=list,
        description="没有行情的 symbol。两种来源：请求了但不在候选清单里的，"
        "以及清单里有、却在全部行情源上都查不到的（如没有交易池的 BVT、"
        "不可转让的 BGT）。后者是事实而非故障，所以单独列出而不是当成 0",
    )
