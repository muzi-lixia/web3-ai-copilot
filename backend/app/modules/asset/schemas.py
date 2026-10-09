"""钱包资产契约（功能点 1：钱包资产查询）。

范围：回答"这个地址持有哪些币、各多少、值多少钱、各占多少"。

**估值是附加层，不是主体。** 余额来自自家 RPC，估值来自第三方行情源 ——
后者的可用性不归我们控制。所以两者在契约里分开表达：

    total_value_usd    只统计**取到价格**的那部分，不是"这个钱包的全部身家"
    has_valuation      是否至少有一个持仓取到了价格
    missing_price      持仓中取不到价格的 symbol

三个字段缺一不可。只给 total_value_usd 的话，行情全挂时它会安静地变成 0，
而 0 在界面上读起来就是"这个钱包是空的"。
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_serializer

from app.common.schemas import DataQuality, Schema


class Asset(Schema):
    """单个资产条目。"""

    symbol: str
    contract: str | None = Field(default=None, description="ERC-20 合约地址；null 表示该链原生币")
    amount: Decimal | None = Field(description="已按 decimals 归一化的人类可读数量")
    decimals: int
    kind: Literal["native", "erc20"] = Field(description="由 contract 是否为 null 推导，不是链上原生字段")

    price_usd: Decimal | None = Field(default=None, description="单价；null 表示该币没有行情")
    value_usd: Decimal | None = Field(default=None, description="amount × price_usd；null 表示无价可算")
    percentage: float | None = Field(
        default=None,
        description="占**有价资产总额**的百分比（0-100）。无价资产为 null —— 不是 0，"
        "因为「占比 0%」和「算不出来」是两回事；总额本身为 0 时同样为 null",
    )

    @field_serializer("amount", "price_usd", "value_usd", when_used="json")
    def _decimal_as_plain_string(self, value: Decimal | None) -> str | None:
        """序列化成十进制字符串。

        两个原因：
        1. 这些值是 bigint 归一化/相乘的结果，走 JSON number 会让 JS 的
           float64 在大额上掉精度（0.1 wei 级的差错会在累加后放大）。
        2. `format(value, "f")` 强制普通小数写法。直接 str(Decimal) 对极小的数
           会输出 `1E-18` 这种科学计数法，前端会原样显示出来。
        """
        return None if value is None else format(value, "f")


class WalletAssets(DataQuality):
    """钱包资产响应，继承统一的数据完整度及问题列表。

    余额与报价分别表达可用性：缺价不丢余额，有价部分总额不代表完整身家。
    computed_at 是本次组装时间，不能代替各报价的真实新鲜度。
    """

    address: str = Field(description="EIP-55 checksum 格式")
    chain_id: int
    chain_name: str
    explorer: str = Field(description="区块浏览器根地址，前端据此拼合约/地址链接")
    assets: list[Asset] = Field(description="按 token 清单顺序，含余额为 0 的条目")

    total_value_usd: Decimal = Field(
        default=Decimal(0),
        description="**有价部分**的合计。无价资产不计入 —— 按 0 计会让总额虚低，而按估值计又无从得知它是多少",
    )
    has_valuation: bool = Field(
        default=False,
        description="是否至少有一个非零持仓取到了价格。为 false 时 total_value_usd "
        "没有意义（它是 0，但不代表钱包是空的），界面必须据此显示「估值不可用」",
    )
    missing_price: list[str] = Field(
        default_factory=list,
        description="**持仓中**没有行情的 symbol。零余额的币不列：它不影响估值，列进来只会让提示栏被噪音塞满",
    )
    stale: bool = Field(default=False, description="行情降级到过期缓存时为 true")
    computed_at: datetime
