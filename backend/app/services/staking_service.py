"""质押仓位（功能点 4）：把链上仓位、Beep 收益与 USD 估值组装成契约。

## 五个来源、五种失败语义

    链上仓位读取（自家 RPC）        失败即抛 —— 没有它这个功能就没有主体
    底层资产行情（第三方）          降级空表
    区间年化（Beep）                降级 null
    累计收益（Beep）                降级 null
    资产侧估值（复用资产服务）       降级 null

后四项都只是**注解**。任何一项失效都不该让"你质押了多少"这个答案一起消失 ——
与资产接口对行情源的处理是同一条口径。

## 两个入口，而不是一个

    get_summary()        质押页用：仓位 + 年化 + 收益 + **质押占比**
    get_staked_slices()  风险报告用：只要把仓位折成风险口径的几行

风险报告不需要占比（它自己算），也不需要资产侧的值 —— 它手上本来就有一份。
如果只提供一个入口，风险报告为了拿仓位会顺带多读一次资产，是纯粹白做的功。
两者共用同一个 `_build_positions`，不会出现两套算法。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from fastapi.concurrency import run_in_threadpool

from app.constants.chains import ChainMeta, get_chain
from app.constants.staking import StakingModule, modules_for
from app.constants.tokens import TokenMeta, tokens_for
from app.core.errors import UpstreamError
from app.core.logging import get_logger
from app.schemas.market import TokenMarket
from app.schemas.staking import (
    PendingWithdrawal,
    StakingEarnings,
    StakingPosition,
    StakingSummary,
)
from app.services import asset_service, beep, staking_vault
from app.services import chain as chain_service
from app.services import market_service
from app.services.chain import to_checksum
from app.services.units import to_human

logger = get_logger(__name__)

_RATE_PLACES = Decimal("1e-18")
"""汇率保留位数。

18 位是底层定点的天然精度（WBERA 就是 18 位小数），再往下是除法产生的循环小数噪音。
这不是显示层的裁剪 —— 显示裁剪仍由前端决定。
"""

_RATIO_PLACES = Decimal("0.0001")
"""占比保留 4 位（0.01% 精度），与 `valuation.py` 两位百分数是同一量级的取舍。"""


@dataclass(frozen=True, slots=True)
class StakedSlice:
    """质押仓位在风险口径里的一行。

    `symbol` 用**份额凭证**（sWBERA）而不是底层资产：它是一笔独立仓位，
    风险报告要能把它单独列出来。分类却按**底层资产**走（见 `underlying_symbol`）——
    sWBERA 的涨跌完全跟随 WBERA，把它当成一类新资产只会多出一个没有意义的类别。
    """

    symbol: str
    underlying_symbol: str
    value_usd: Decimal


@dataclass(slots=True)
class _Positions:
    """一次仓位构建的结果。两个入口共用。"""

    positions: list[StakingPosition] = field(default_factory=list)
    staked_value_usd: Decimal = Decimal(0)
    has_valuation: bool = False
    missing_price: list[str] = field(default_factory=list)
    stale: bool = False


async def get_summary(chain_id: int, owner: str) -> StakingSummary:
    """质押页入口：仓位 + 年化 + 收益 + 质押占比。"""
    chain = get_chain(chain_id)
    address = to_checksum(owner)
    now = datetime.now(UTC)

    # 占比的分母需要资产侧，所以这里是唯一会读资产的地方。
    built, liquid = await asyncio.gather(
        _build_positions(chain_id, address, now),
        _load_liquid_value(chain_id, address),
    )

    return StakingSummary(
        address=address,
        chain_id=chain.chain_id,
        chain_name=chain.name,
        explorer=chain.explorer,
        positions=built.positions,
        staked_value_usd=built.staked_value_usd,
        has_valuation=built.has_valuation,
        missing_price=built.missing_price,
        stale=built.stale,
        liquid_value_usd=liquid,
        portfolio_ratio=_ratio(built.staked_value_usd, liquid),
        computed_at=now,
    )


async def get_staked_slices(chain_id: int, owner: str) -> tuple[StakedSlice, ...] | None:
    """风险报告入口：把仓位折成风险口径的几行。

    返回 `None` 表示**不可知**（有仓位却取不到价），与返回空元组（测出来是零）
    是两件事 —— 前者占比该显示「—」，后者该显示 0%。
    """
    chain = get_chain(chain_id)
    built = await _build_positions(chain_id, to_checksum(owner), datetime.now(UTC))

    if built.missing_price:
        # 有仓位但一个价都取不到：此时 staked_value_usd 恒为 0，
        # 直接被当成"确实没质押"就错了。
        return None

    # 该链没登记质押模块时 positions 为空 → 空元组，即"测过，没有"。
    return tuple(
        StakedSlice(
            symbol=position.share_symbol,
            underlying_symbol=position.underlying_symbol,
            value_usd=position.value_usd or Decimal(0),
        )
        for position in built.positions
    )


async def _build_positions(chain_id: int, address: str, now: datetime) -> _Positions:
    """读链上 + 取行情/年化/收益，组装出仓位表。"""
    chain = get_chain(chain_id)
    modules = modules_for(chain_id)
    if not modules:
        # 该链还没接质押。不是错误，也不值得为它多打一次查询。
        return _Positions()

    readings, quotes, apy, earnings = await asyncio.gather(
        run_in_threadpool(_read_positions, chain, modules, address),
        _load_quotes(chain, modules),
        beep.get_stake_apy_safe(),
        beep.gather_earnings([module.vault for module in modules], address),
    )

    built = _Positions()
    for module in modules:
        reading = readings.get(module.key)
        if reading is None:
            # 单个模块读不出来：跳过它而不是让整页消失。日志里留痕 ——
            # "少了一个模块"在界面上看不出是读失败还是真的没仓位。
            logger.error("质押模块 %s 读取失败，本次跳过", module.key)
            continue

        underlying = _resolve_underlying(chain_id, module, reading.asset)
        quote = quotes.get(underlying.symbol)
        price = quote.price_usd if quote is not None else None
        built.stale = built.stale or (quote.stale if quote is not None else False)

        # 链上读不到解绑时长时退回注册表声明值，而不是当成 0 ——
        # 那会让所有排队中的请求立刻显示"已可提取"。
        cooldown = (
            reading.cooldown_seconds
            if reading.cooldown_seconds is not None
            else module.unbonding_seconds
        )

        # 排队中的锁定量也计入仓位：解绑期一过它就是可提取的资产，
        # 不会因为"正在排队"而从身家里消失。份额在排队时已烧掉，
        # 所以它既不在 balanceOf 里、也不在资产页余额里 —— 只在这里出现一次。
        claim_units = reading.underlying + sum(item.assets for item in reading.pending)
        claim_amount = to_human(claim_units, underlying.decimals)

        value = None if price is None else claim_amount * price
        if value is not None:
            built.staked_value_usd += value
            if claim_units != 0:
                built.has_valuation = True
        elif claim_units != 0:
            # 只报**有仓位**却没价的：空仓位没价不影响任何判断。
            built.missing_price.append(underlying.symbol)

        built.positions.append(
            StakingPosition(
                module_key=module.key,
                name=module.name,
                protocol=module.protocol,
                share_symbol=module.share_symbol,
                shares=to_human(reading.shares, module.share_decimals),
                underlying_symbol=underlying.symbol,
                underlying_amount=to_human(reading.underlying, underlying.decimals),
                exchange_rate=_exchange_rate(reading),
                price_usd=price,
                value_usd=value,
                apy=apy.apy if apy is not None else None,
                apy_interval=apy.interval if apy is not None else None,
                earnings=_to_earnings(earnings.get(module.vault.lower()), underlying.decimals),
                pending_withdrawals=[
                    _to_pending(item, module, underlying, cooldown, price, now)
                    for item in reading.pending
                ],
                unbonding_seconds=cooldown,
            )
        )

    # 两个模块共用同一个底层资产时会出现重复项。
    built.missing_price = list(dict.fromkeys(built.missing_price))
    return built


def _to_pending(
    item: staking_vault.PendingWithdrawal,
    module: StakingModule,
    underlying: TokenMeta,
    cooldown: int,
    price: Decimal | None,
    now: datetime,
) -> PendingWithdrawal:
    """一笔排队中的赎回 → 契约。金额锁定，所以它不随汇率上涨。"""
    assets = to_human(item.assets, underlying.decimals)
    unlock_at = item.unlock_at(cooldown)
    return PendingWithdrawal(
        request_id=item.request_id,
        assets=assets,
        # 份额与底层的精度不同，各用各的 decimals —— 混用会让份额差好几个数量级。
        shares=to_human(item.shares, module.share_decimals),
        value_usd=None if price is None else assets * price,
        requested_at=datetime.fromtimestamp(item.requested_at, UTC),
        unlock_at=datetime.fromtimestamp(unlock_at, UTC),
        ready=now.timestamp() >= unlock_at,
        receiver=item.receiver,
    )


def _read_positions(
    chain: ChainMeta, modules: tuple[StakingModule, ...], address: str
) -> dict[str, staking_vault.VaultReading]:
    """读该链上全部质押模块（同步阻塞，由调用方丢线程池）。

    每个模块单独 try：一个模块的合约升级换了方法名，不该让另一个模块的仓位也读不出来。
    但**全部失败时抛错** —— 那种情况说明 RPC 或读取层坏了，返回空字典会被界面
    读成"你没有质押"，是把故障伪装成事实。
    """
    w3 = chain_service.connect(chain)
    out: dict[str, staking_vault.VaultReading] = {}
    failures: list[str] = []

    for module in modules:
        try:
            out[module.key] = staking_vault.read_vault(w3, chain, module, address)
        except Exception as exc:  # noqa: BLE001 —— 合约异常类型很多，逐个记录后继续
            failures.append(f"{module.key} ({type(exc).__name__}: {exc})")
            logger.warning("质押模块 %s 读取失败：%s", module.key, exc)

    if not out:
        raise UpstreamError(f"{chain.name} 的质押模块全部读取失败：{'; '.join(failures)}")
    return out


def _resolve_underlying(chain_id: int, module: StakingModule, onchain_asset: str) -> TokenMeta:
    """确定仓位的底层资产条目（要它的 symbol 与 decimals）。

    **以链上 `asset()` 返回的地址为准**，注册表里声明的 symbol 只是兜底：
    链告诉我们的才是事实，注册表是手写的、会漂。两者不一致时打警告并按链上的走 ——
    静默采用任一方的结果，都会让"某个数小了一半"这种错无迹可寻。
    """
    tokens = tokens_for(chain_id)

    if onchain_asset:
        matched = next(
            (
                token
                for token in tokens
                if token.address and token.address.lower() == onchain_asset.lower()
            ),
            None,
        )
        if matched is not None:
            if matched.symbol != module.underlying_symbol:
                logger.warning(
                    "质押模块 %s 注册表声明底层为 %s，链上 asset() 对应的是 %s；按链上的走",
                    module.key,
                    module.underlying_symbol,
                    matched.symbol,
                )
            return matched
        logger.warning(
            "金库 %s 的 asset()=%s 不在 token 清单里，退回注册表声明的 symbol",
            module.vault,
            onchain_asset,
        )
    else:
        logger.warning("没能读到金库 %s 的 asset()，退回注册表声明的 symbol", module.vault)

    declared = next((token for token in tokens if token.symbol == module.underlying_symbol), None)
    if declared is None:
        # 走到这里就是配置错了。显式抛错而不是猜一个精度 ——
        # 猜出来的 decimals 会让数量差几个数量级，而且不报错。
        raise UpstreamError(
            f"质押模块 {module.key} 的底层资产 {module.underlying_symbol} "
            f"不在链 {chain_id} 的 token 清单里，无法归一化数量"
        )
    return declared


def _exchange_rate(reading: staking_vault.VaultReading) -> Decimal | None:
    """1 份份额值多少底层。

    用金库全局的 totalAssets / totalSupply 而不是用户自己的 underlying / shares：
    两者数学上相等，但前者在用户份额为 0 时依然有值 —— 而"这个金库现在什么价"
    恰恰是没持仓的人最想知道的。
    """
    if reading.total_supply <= 0:
        return None
    return (Decimal(reading.total_assets) / Decimal(reading.total_supply)).quantize(
        _RATE_PLACES, rounding=ROUND_HALF_UP
    )


def _to_earnings(raw: beep.StakeEarnings | None, decimals: int) -> StakingEarnings | None:
    """Beep 的原始整数收益 → 契约。取不到时 null，不是 0。"""
    if raw is None:
        return None
    return StakingEarnings(
        total=to_human(raw.total, decimals),
        realized=to_human(raw.realized, decimals),
        unrealized=to_human(raw.unrealized, decimals),
    )


def _ratio(staked: Decimal, liquid: Decimal | None) -> float | None:
    """质押 ÷（质押 + 资产）。

    资产侧取不到时给 None，而不是"只拿质押当分母算成 100%" ——
    那会把一个"没测到"变成"全部都是质押"，方向上是错的。
    组合价值为 0 时同样给 None：分母不存在。
    """
    if liquid is None:
        return None
    total = staked + liquid
    if total <= 0:
        return None
    return float((staked / total).quantize(_RATIO_PLACES, rounding=ROUND_HALF_UP))


async def _load_quotes(chain: ChainMeta, modules: tuple[StakingModule, ...]) -> dict[str, TokenMarket]:
    """取底层资产行情。失败返回空表，**不向上抛**（行情是注解层）。"""
    symbols = sorted({module.underlying_symbol for module in modules})
    try:
        result = await market_service.get_quotes(chain.chain_id, symbols)
    except UpstreamError as exc:
        logger.warning("底层资产行情不可用，本次只返回数量：%s", exc)
        return {}
    return {quote.symbol: quote for quote in result.tokens}


async def _load_liquid_value(chain_id: int, address: str) -> Decimal | None:
    """取未质押资产的价值合计（占比分母的另一半）。失败返回 None。"""
    try:
        wallet = await asset_service.get_wallet_assets(chain_id, address)
    except UpstreamError as exc:
        logger.warning("资产侧不可用，本次不给出质押占比：%s", exc)
        return None
    return wallet.total_value_usd
