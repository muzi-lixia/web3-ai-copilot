"""质押模块注册表。

**一个 `StakingModule` = 一个可质押的东西。** 加模块 = 加一条，读取层、服务层、
路由都不用改 —— 与 `chains.py`（链）、`tokens.py`（币）是同一套口径。

为什么单独一份而不是并进 tokens.py：质押凭证（sWBERA）**故意不进 token 清单**。
它若进了清单，资产页和质押页会各展示一次同一个仓位，`total_value_usd` 也会
把它算进去一次，于是风险报告里的「质押占比」会算出大于 100% 的数。
质押是资产之上的一层状态，不是又一个币。

⚠️ 与 tokens.py 同样的规矩：**地址、时长写错不会报错，只会静默读到空**。
新增条目必须跑 `scripts/verify_constants.py` 与链上核对（它会比对本文件声明的
解绑时长与链上 `WITHDRAWAL_COOLDOWN()`）。
"""

from dataclasses import dataclass
from typing import Literal

from app.config.constants.chains import BERACHAIN
from app.shared.errors import UnsupportedChainError

ReadKind = Literal["erc4626_queue"]
"""读取方式。目前只有一种形态：

    erc4626_queue = ERC-4626 金库 + 独立提款请求合约
        份额 ERC-20 直接 balanceOf；数量要走 convertToAssets 折算（**份额 ≠ 底层**）；
        赎回不平滑，而是排队 → 等解绑 → 再手动完成，解绑期内资产冻结、不计收益。

留着这个字段是因为"质押"的形态会分化（单币质押 / LP 凭证挖矿 / 锁仓合约），
不同形态的读取方式毫无共同点。届时按 kind 分派，不动服务层。
"""


@dataclass(frozen=True, slots=True)
class StakingModule:
    key: str
    """短名，用作日志与前端条目标识。"""

    chain_id: int

    name: str
    """展示名。"""

    protocol: str
    """收益来源写在这里 —— 用户看到的 APY 是**谁**给的，比 APY 是多少更要紧。"""

    vault: str
    """金库合约地址（ERC-4626）。份额与底层资产的折算关系由它决定。"""

    share_symbol: str
    """份额凭证符号。前端要显示"你持有的是它，不是底层币"。"""

    share_decimals: int
    """份额的归一化位数。与 `tokens.py` 里的 decimals 同理：存下来直接用，
    省掉每次都去链上问一遍；核对脚本会与链上 `decimals()` 比对。"""

    underlying_symbol: str
    """底层资产符号。

    **必须能在 `tokens.py` 的该链清单里查到** —— 质押的 USD 估值就是借它的价格算的。
    查不到不会报错，只会让这个仓位的估值永远是 null。
    """

    unbonding_seconds: int
    """解绑时长（秒）。

    链上是常数（`WITHDRAWAL_COOLDOWN()`），这里登记的是**声明值**，
    由核对脚本与链上实际值比对。写错会让"还有多久能提"整个算错，
    而界面上的日期看起来照样合理。
    """

    read_kind: ReadKind = "erc4626_queue"


BERACHAIN_STAKING: tuple[StakingModule, ...] = (
    StakingModule(
        key="bera-pool-v2",
        chain_id=BERACHAIN.chain_id,
        name="BERA 质押金库",
        protocol="Berachain PoL v2",
        # 四处独立来源交叉确认：BeraScan 合约页（POL Staked WBERA / sWBERA）、
        # DefiLlama（symbol=sWBERA、decimals=18）、Berachain skunkworks 的 bearn CLI、
        # 以及 Beep 的 /v1/stake/{vault}/stats-by-day 按该地址返回了逐日数据。
        vault="0x118D2cEeE9785eaf70C15Cd74CD84c9f8c3EeC9a",
        share_symbol="sWBERA",
        share_decimals=18,
        # 金库的底层是 WBERA 而不是原生 BERA：存原生币时合约内部先包一层。
        # 所以估值走 WBERA 的价，而不是 BERA 的（两者 1:1，价格本就相同，
        # 但写 WBERA 才是链上真实的那个 asset()）。
        underlying_symbol="WBERA",
        unbonding_seconds=7 * 24 * 60 * 60,
    ),
)


STAKING_BY_CHAIN: dict[int, tuple[StakingModule, ...]] = {
    BERACHAIN.chain_id: BERACHAIN_STAKING,
}


def modules_for(chain_id: int) -> tuple[StakingModule, ...]:
    """取该链登记的质押模块。未登记返回空元组。

    这里**不抛错**，与 `tokens_for` 的处理相反：代币清单为空意味着"这条链什么都读不出来"，
    是配置缺失；而质押模块为空是一个完全正常的中间状态 ——
    新加的链还没接质押，资产功能照样可用。
    """
    return STAKING_BY_CHAIN.get(chain_id, ())


def module_for(chain_id: int, key: str) -> StakingModule:
    """按 key 取单个模块。找不到抛 UnsupportedChainError。"""
    for module in modules_for(chain_id):
        if module.key == key:
            return module
    raise UnsupportedChainError(f"链 {chain_id} 上没有登记质押模块 {key}")
