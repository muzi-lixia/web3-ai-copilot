"""风险报告契约（模块 6）。

**契约层面的硬约束：除 `explanation` 外，所有字段都由代码计算，LLM 不参与。**
理由：数值若由模型生成则不可复现、不可测试、无法解释；LLM 只负责把算好的数字写成自然语言。

## 组合口径包含质押仓位

风险口径是「钱包里的币 + 金库里的仓位」，两者都是持有。质押那一行按**底层资产**
分类（sWBERA 跟随 WBERA），所以它计入波动/稳定币比，而不是被漏掉。

于是有**两套各自完整、但回答不同问题的切分**：

    stablecoin_ratio + volatile_ratio = 1     按价格波动性
    liquid_ratio    + staking_ratio   = 1     按能否即时动用

界面上必须写清是哪一套，否则"四个数加起来不是 100%"会被当成 bug。

## null 与 0 的分界

    staking_ratio / liquid_ratio   测过确实是 0 → 0；**没测到 → null**
    explanation                    LLM 解释属功能点 5，现在恒为 null

`null` 表示"没测"，`0` 表示"测出来是零"。它们指向的处理方式完全相反：
前者界面要显示「—」并说明原因，后者是事实。合成一个值就没有这个区分了。
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.common import Schema

RiskLevel = Literal["low", "medium", "high", "unknown"]
"""风险档。

`unknown` 不是"第三种风险"，而是**输入不足** —— 钱包里没有可估值的持仓时，
任何档位都是编出来的。三种风险档的具体判据见 `services/risk_service.py` 的阈值常量。
"""


class RiskReport(Schema):
    # ── 以下全部由代码计算 ───────────────────────────────
    top_asset: str | None = Field(
        default=None, description="估值占比最高的资产 symbol；无可估值持仓时为 null"
    )
    top_asset_ratio: float | None = Field(default=None, description="0-1")
    concentration_score: float = Field(
        default=0.0,
        description="**波动资产内部**的集中度 0-100（HHI 归一化）。剔除稳定币后再归一 —— "
        "全押稳定币的价值波动敞口接近 0，算进去会让指标与风险档自相矛盾",
    )
    stablecoin_ratio: float = Field(default=0.0, description="0-1")
    volatile_ratio: float = Field(default=0.0, description="0-1，高波动资产占比")
    staking_ratio: float | None = Field(
        default=None,
        description="0-1，质押价值占**整个组合**（质押 + 未质押资产）的比例。"
        "有仓位却取不到价时为 null —— 那种情况下它是一个取不到的 0，不是真的没有质押",
    )
    liquid_ratio: float | None = Field(
        default=None, description="0-1，可即时动用部分；与 staking_ratio 同进同退，两者之和为 1"
    )
    risk_level: RiskLevel = Field(default="unknown")
    computed_at: datetime

    # ── LLM 只填这一项 ──────────────────────────────────
    explanation: str | None = Field(
        default=None,
        description="由 LLM 基于上述数字生成的自然语言解释；失败时为 None，不影响报告可用",
    )
