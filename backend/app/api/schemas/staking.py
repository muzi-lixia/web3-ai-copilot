"""质押仓位契约（功能点 4）。

覆盖 Berachain PoL v2 的 BERA 质押金库：存 BERA / WBERA 拿到 sWBERA 份额，
收益由 PoL 激励回购成 WBERA 注入金库、**自动复利**（体现为份额单价上涨，不发新币），
赎回要排队、等 7 天解绑、再手动完成。

## 三个来源，三种可靠性，分开表达

    shares / underlying / pending_withdrawals   链上直读，精确
    earnings                                    链上事件回溯算出的累计值（Beep）
    apy                                         区间年化（Beep），**不是承诺**

## 契约里放什么、不放什么

- **放** `exchange_rate`：1 份额值多少底层。它是这个仓位最需要解释的一个数 ——
  sWBERA 与 WBERA 不是 1:1（实测 1 sWBERA ≈ 1.467 WBERA），
  用户看到"我存了 100 个，怎么变成 146 个了"必须有处可查。
- **不放** `unlock_note` 这类"没有值就写句说明"的字段：说明是界面的事，
  契约只该有值。旧的 `provider: liquid | custom` 同理 —— 那是实现分类，
  前端拿到它也无法据此做任何分支。
- `apy` 与 `apy_interval` **同进同退**。只说"年化 6.4%"而不说窗口，
  等于把最容易被误读的那个量藏起来：6 小时窗口和 7 天窗口是两个数。
"""

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_serializer

from app.api.schemas.common import DataQuality, Schema

_DECIMAL_FIELDS = ("total", "realized", "unrealized")


class StakingEarnings(Schema):
    """地址在该金库上的**累计**收益，来自 Beep。

    三个数分开，是因为它们回答的是不同问题：累计赚了多少 / 其中已赎回到手多少 /
    其中还押在金库里的浮盈多少。合成一个数会让"我赚了多少"的答案取决于
    有没有赎回，而用户问的通常不是这个。
    """

    total: Decimal
    realized: Decimal = Field(description="已落袋：随赎回一起结算的部分")
    unrealized: Decimal = Field(description="仍押在金库里的浮盈")

    @field_serializer(*_DECIMAL_FIELDS, when_used="json")
    def _decimal_as_plain_string(self, value: Decimal) -> str:
        return format(value, "f")


class PendingWithdrawal(Schema):
    """一笔排队中的赎回。

    份额在排队时**就已经烧掉**（合约这么设计，为了不让排队期间继续吃收益），
    所以它既不在 `shares` 里、也不在资产页的余额里 —— 只在这里。
    """

    request_id: int
    assets: Decimal = Field(description="请求发起时锁定的底层数量，解绑期内不再增长")
    shares: Decimal = Field(description="发起时烧掉的份额")
    value_usd: Decimal | None = Field(default=None, description="按当前价折算；无价时为 null")
    requested_at: datetime
    unlock_at: datetime = Field(description="requested_at + 解绑时长")
    ready: bool = Field(description="是否已过解绑期、可以完成提取")
    receiver: str = Field(description="完成后资产打给谁")

    @field_serializer("assets", "shares", "value_usd", when_used="json")
    def _decimal_as_plain_string(self, value: Decimal | None) -> str | None:
        return None if value is None else format(value, "f")


class StakingPosition(Schema):
    """一个质押模块上的仓位。"""

    module_key: str
    name: str = Field(description="展示名")
    protocol: str = Field(description="收益来源 —— 用户看到的年化是**谁**给的，比年化是多少更要紧")

    share_symbol: str
    shares: Decimal = Field(description="份额数量。它**不是**底层资产数量")
    underlying_symbol: str
    underlying_amount: Decimal = Field(description="按当前汇率折算出的底层数量")
    exchange_rate: Decimal | None = Field(
        default=None,
        description="1 份份额值多少底层。**必须展示** —— 它是份额涨价这个收益形式的唯一解释。"
        "金库份额总量为 0 时为 null（分母不存在，比值没有定义）",
    )

    price_usd: Decimal | None = Field(default=None, description="底层资产单价；null 表示取不到行情")
    value_usd: Decimal | None = Field(
        default=None,
        description="(份额折算量 + 排队中的锁定量) × 价格。排队中的也算 —— "
        "解绑期一过它就是可提取的资产，不会因为正在排队而消失",
    )

    apy: float | None = Field(
        default=None, description="年化**比值**（0.0644 表示 6.44%），不是百分数；取不到时为 null"
    )
    apy_interval: str | None = Field(
        default=None, description="年化窗口（ONE_DAY / SEVEN_DAYS / …）；与 apy 同进同退"
    )

    earnings: StakingEarnings | None = Field(
        default=None, description="累计收益；Beep 表示无法精确计算时为 null（不是 0）"
    )
    pending_withdrawals: list[PendingWithdrawal] = Field(default_factory=list)
    unbonding_seconds: int = Field(
        description="解绑时长。界面上给提示，链上读不到时也可拿它兜底"
    )

    @field_serializer(
        "shares", "underlying_amount", "exchange_rate", "price_usd", "value_usd", when_used="json"
    )
    def _decimal_as_plain_string(self, value: Decimal | None) -> str | None:
        """金额序列化成十进制字符串，理由同 `schemas/wallet.py` 的 amount。"""
        return None if value is None else format(value, "f")


class StakingSummary(DataQuality):
    address: str
    chain_id: int
    chain_name: str
    explorer: str
    positions: list[StakingPosition]

    staked_value_usd: Decimal = Field(
        default=Decimal(0), description="质押仓位合计（**有价部分**，无价仓位不计入）"
    )
    has_valuation: bool = Field(
        default=False,
        description="是否至少有一个非零质押仓位取到了价格。为 false 时 staked_value_usd "
        "没有意义，界面必须显示「估值不可用」而不是 $0.00",
    )
    missing_price: list[str] = Field(
        default_factory=list, description="有仓位却取不到价格的底层资产 symbol"
    )
    stale: bool = Field(default=False, description="行情降级到过期缓存时为 true")

    liquid_value_usd: Decimal | None = Field(
        default=None,
        description="资产侧（未质押）的估值合计，用来解释 portfolio_ratio 的分母。"
        "资产接口取失败时为 null —— 占比跟着一起为 null，不单独编一个分母出来",
    )
    portfolio_ratio: float | None = Field(
        default=None,
        description="质押价值 ÷（质押价值 + 资产价值），0-1。null 有两种含义："
        "资产侧取不到，或整个组合价值为 0；两种情况下这个比值都没有定义",
    )
    computed_at: datetime

    @field_serializer("staked_value_usd", "liquid_value_usd", when_used="json")
    def _decimal_as_plain_string(self, value: Decimal | None) -> str | None:
        return None if value is None else format(value, "f")
