"""资产风险分析（功能点 3）。

**全部由代码计算，不调用 LLM。** 数值若由模型生成就不可复现、不可测试、
也没法向用户解释它是怎么来的 —— 这一层只负责算数字，把它写成自然语言是功能点 5 的事。

拆成两个函数，理由与 `valuation.py` 相同：

    build_report(wallet, staked)   纯函数，可单测
    assess(chain, owner)           取数据 + 调上面那个

## 组合口径：质押仓位算持仓

功能点 4 接入质押后，风险口径从「钱包里的币」扩成「钱包里的币 + 金库里的仓位」——
两者都是持有，只是一个在手上、一个在质押。质押的那一行按**底层资产**分类
（sWBERA 跟随 WBERA），所以它计入波动/稳定币比，而不是新开一档。

于是有**两套互补切分**，各自都完整，但回答的问题不同：

    稳定币 + 高波动 = 100%     按价格波动性切（质押仓位算在它底层资产那一类）
    可动用 + 质押   = 100%     按流动性切

界面上要写清楚是"哪套切分"，否则四个百分比加起来不是 100% 会被当成 bug。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from app.common.schemas import DataIssue
from app.common.units import monetary_calculation
from app.core.exceptions import UpstreamError
from app.core.logging import get_logger
from app.modules.asset import service as asset_service
from app.modules.asset.schemas import WalletAssets
from app.modules.asset.tokens import RiskClass, risk_class_of
from app.modules.risk.schemas import RiskLevel, RiskReport
from app.modules.staking import service as staking_service

logger = get_logger(__name__)

# ── 风险判据阈值 ────────────────────────────────────────────
# 三条独立判据，各自只回答"这一条算不算风险"，**命中条数直接决定档位**。
# 刻意不用加权求和：权重是拍脑袋来的，而"命中几条"任何人都能拿计算器复核，
# 也能在界面上逐条解释给用户看。
_CONCENTRATION_HIGH = 70.0
"""HHI 归一化后 ≥ 70。HHI = 0.70 约等于头部资产占到 84% 的权重。"""

_VOLATILE_HIGH = 0.8
"""高波动资产占比 ≥ 80%。"""

_STABLECOIN_THIN = 0.1
"""稳定币缓冲 < 10%。行情剧烈时没有可动用而不割肉的仓位。"""


def _level(concentration: float, stablecoin: float, volatile: float) -> RiskLevel:
    """三条判据命中几条 → 档位。两条及以上算 high。"""
    hits = 0
    if concentration >= _CONCENTRATION_HIGH:
        hits += 1
    if volatile >= _VOLATILE_HIGH:
        hits += 1
    if stablecoin < _STABLECOIN_THIN:
        hits += 1

    if hits >= 2:
        return "high"
    return "medium" if hits == 1 else "low"


@monetary_calculation
def build_report(
    wallet: WalletAssets,
    staked: tuple[staking_service.StakedSlice, ...] | None = None,
) -> RiskReport:
    """由持仓（含估值）与质押仓位算出风险报告。纯函数。

    `staked` 三态，各有含义，不能混：

        `(…)`      有质押模块，价值已算出（空元组 = 测过，确实没质押）
        `None`     **不可知**（没测、读不出来，或有仓位却取不到价）→ 占比给 null

    默认值是 `None` 而不是空元组：不传参数的调用方**没有查过**质押，
    给它 0% 就等于替它宣布"这个钱包没质押"。宁可显示「—」。
    """
    chain_id = wallet.chain_id
    now = datetime.now(UTC)
    issues = list(wallet.issues)
    if wallet.status != "complete" and not issues:
        issues.append(DataIssue(code="wallet_incomplete", message="钱包数据不完整"))
    for asset in wallet.assets:
        if asset.amount is None or (asset.amount != 0 and asset.value_usd is None):
            issues.append(
                DataIssue(
                    code="asset_incomplete", asset=asset.symbol, message=f"{asset.symbol} 余额或估值不可用"
                )
            )
    if wallet.missing_price and not issues:
        issues.append(DataIssue(code="price_unavailable", message="部分持仓缺少报价"))
    if staked is None:
        issues.append(DataIssue(code="staking_unavailable", message="质押仓位或估值不可用"))
    # 组合输入不完整时直接返回 unavailable，不能对剩余有价资产计算一个完整风险结论。
    if issues:
        return RiskReport(status="unavailable", issues=issues, computed_at=now)

    # 组合 = 未质押资产 + 质押仓位。占比在这里自己算，不复用 Asset.percentage ——
    # 后者的分母是"有价资产"，不含质押，用它算出来的占比在合并口径下偏大。
    entries: list[tuple[str, Decimal, RiskClass]] = []
    for asset in wallet.assets:
        if asset.value_usd is None or asset.value_usd <= 0:
            continue
        entries.append((asset.symbol, asset.value_usd, risk_class_of(chain_id, asset.symbol)))

    staked_value = Decimal(0)
    for slice_ in staked or ():
        if slice_.value_usd <= 0:
            continue
        # 分类跟着底层资产走：sWBERA 的价格完全由 WBERA 决定，
        # 给它单开一类只会造出一个没有解释力的类别。
        entries.append((slice_.symbol, slice_.value_usd, risk_class_of(chain_id, slice_.underlying_symbol)))
        staked_value += slice_.value_usd

    total = sum((value for _, value, _ in entries), Decimal(0))
    if total <= 0:
        # 没有可估值的持仓。此时任何档位都是编的，如实给 unknown。
        # 两个流动性比率仍然可以给：测过了，且组合为空 → 都不存在，给 null。
        return RiskReport(risk_level="unknown", computed_at=now)

    # 占比从这一行起**一律是 float**，必须在这里一次性定死类型。
    #
    # 因为 Decimal 与 float 在 Python 里**不能混算**：`1.0 - Decimal("0.5")` 直接抛
    # TypeError（不是悄悄降精度）。`value_usd` 是 Decimal，所以 value/total 也是 Decimal，
    # 再拿它去减 1.0 或乘 100.0 就会炸 —— 这就是这个接口之前必然 500 的原因。
    #
    # 边界正好落在这里：**金额侧（value_usd）继续用 Decimal，比率侧从这行起是 float**，
    # 与 schemas/risk.py 里这几个字段声明为 number 一致。
    # 除法放在 Decimal 侧做完再转 float（精度比 float/float 好），只在最后一步降级 ——
    # `staking_service._ratio()` 是同一套写法。
    shares = [(symbol, float(value / total), klass) for symbol, value, klass in entries]

    # 起始值显式给 0.0：空生成器时 sum 返回的是 int 0，`1.0 - 0` 虽然碰巧能跑，
    # 但那是在靠运气。类型一致就写出来。
    stablecoin = sum((share for _, share, klass in shares if klass == "stable"), 0.0)
    # 两类互斥且穷尽（见 modules/asset/tokens.py 的 RiskClass），所以波动比取补集即可。
    # 不重新遍历一遍求和，就不会出现"两者之和 ≠ 1"这种由两次独立计算引入的偏差。
    volatile = 1.0 - stablecoin

    # 集中度：**波动资产内部**的 HHI（Σ share²），再乘 100 落到契约的 0-100。
    #
    # 为什么把稳定币剔除后再算：集中度在这里是"风险敞口"的一个侧面。
    # 一个 100% 持有 USDT0 的钱包，对全部资产算 HHI 也是满分 1.0 —— 指标显示
    # "极度集中"，而它其实没有任何价值波动敞口，档位会给成中/高风险，
    # 指标与档位就自相矛盾了。把稳定币从分子分母一起剔掉、对剩下的部分重新归一，
    # 衡量的才是"风险有多集中"。
    #
    # 用 HHI 而不是"第一名占比"：它同时惩罚"一超"和"多强"两种形态 ——
    # 50/50 与 90/5/5 的最大占比相近，后者风险高得多，HHI 分得开（0.50 vs 0.815）。
    exposures: dict[str, Decimal] = {}
    for asset in wallet.assets:
        if asset.value_usd is not None and asset.value_usd > 0:
            key = "BERA" if asset.symbol in {"BERA", "WBERA"} else asset.symbol
            if risk_class_of(chain_id, asset.symbol) != "stable":
                exposures[key] = exposures.get(key, Decimal(0)) + asset.value_usd
    for item in staked or ():
        key = "BERA" if item.underlying_symbol in {"BERA", "WBERA"} else item.underlying_symbol
        if item.value_usd > 0 and risk_class_of(chain_id, item.underlying_symbol) != "stable":
            exposures[key] = exposures.get(key, Decimal(0)) + item.value_usd
    volatile_shares = [float(value / total) for value in exposures.values()]
    volatile_total = sum(volatile_shares, 0.0)
    concentration = (
        sum((share / volatile_total) ** 2 for share in volatile_shares) * 100.0 if volatile_total > 0 else 0.0
    )

    top_symbol, top_ratio, _ = max(shares, key=lambda item: item[1])

    # 流动性切分。staked 为 None 表示"没测到"，此时两个比率都给 null ——
    # 给 0 会让界面显示"质押占比 0%"，读起来就是"确实没质押"。
    if staked is None:
        staking_ratio: float | None = None
        liquid_ratio: float | None = None
    else:
        staking_ratio = float(staked_value / total)
        liquid_ratio = float((total - staked_value) / total)

    return RiskReport(
        top_asset=top_symbol,
        top_asset_ratio=top_ratio,
        concentration_score=concentration,
        stablecoin_ratio=stablecoin,
        volatile_ratio=volatile,
        staking_ratio=staking_ratio,
        liquid_ratio=liquid_ratio,
        risk_level=_level(concentration, stablecoin, volatile),
        computed_at=now,
    )


async def assess(chain_id: int, owner: str) -> RiskReport:
    """取该地址的资产与质押仓位，算出风险报告。

    两件事并发读取。质押不可用时保留响应及原因，但完整组合风险返回 unknown，
    不将剩余钱包资产误当成整个组合。
    """
    wallet, staked = await asyncio.gather(
        asset_service.get_wallet_assets(chain_id, owner),
        _load_staked(chain_id, owner),
    )
    return build_report(wallet, staked)


async def _load_staked(chain_id: int, owner: str) -> tuple[staking_service.StakedSlice, ...] | None:
    """取质押仓位。失败返回 None（= 不可知），不向上抛。"""
    try:
        return await staking_service.get_staked_slices(chain_id, owner)
    except UpstreamError as exc:
        logger.warning("质押仓位不可用，本次不给出质押/流动比：%s", exc)
        return None
