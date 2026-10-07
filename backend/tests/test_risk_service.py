"""`risk_service.build_report` 的单测。

为什么从它开始：它是**纯函数** —— 不吃网络、不碰 RPC，只吃两个契约对象，
所以能直接构造输入、断言输出，不需要起节点也不需要对 HTTP 打桩。

2026-10-07 那个让 `GET /risk/{address}/report` 必然 500 的错误
（`Decimal` 与 `float` 混算）在集成测试里只表现为一个 500，
在这里就是一行断言 —— 新增这个文件就是为了钉住它。

跑法：

    cd backend && pytest
"""

from datetime import UTC, datetime
from decimal import Decimal

from app.schemas.wallet import Asset, WalletAssets
from app.services.risk_service import build_report
from app.services.staking_service import StakedSlice

_NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _wallet(*holdings: tuple[str, str]) -> WalletAssets:
    """按 (symbol, USD 估值) 造一个钱包。

    估值写成**字符串**而不是 float：测试里不出现 float 字面量，
    才看得清「金额是十进制字符串」这条全项目约定。
    """
    assets = [
        Asset(
            symbol=symbol,
            amount=Decimal("1"),
            decimals=18,
            kind="erc20",
            value_usd=Decimal(value),
        )
        for symbol, value in holdings
    ]
    return WalletAssets(
        address="0x" + "11" * 20,
        chain_id=80094,
        chain_name="Berachain",
        explorer="https://berascan.com",
        assets=assets,
        total_value_usd=sum((Decimal(value) for _, value in holdings), Decimal(0)),
        has_valuation=bool(assets),
        computed_at=_NOW,
    )


def test_all_stablecoin_is_low_risk() -> None:
    """全稳定币：波动敞口为 0，集中度也必须是 0 而不是满分。"""
    report = build_report(_wallet(("USDT0", "1000")))

    assert report.stablecoin_ratio == 1.0
    assert report.volatile_ratio == 0.0
    assert report.concentration_score == 0.0
    assert report.risk_level == "low"
    assert report.top_asset == "USDT0"


def test_all_volatile_is_high_risk() -> None:
    """全押波动资产：三条判据全中 → 高风险。"""
    report = build_report(_wallet(("WBERA", "1000")))

    assert report.stablecoin_ratio == 0.0
    assert report.volatile_ratio == 1.0
    assert report.concentration_score == 100.0
    assert report.risk_level == "high"


def test_balanced_wallet_is_medium_risk() -> None:
    """一半稳定币一半波动：只命中「集中度」一条 → 中风险。"""
    report = build_report(_wallet(("USDT0", "500"), ("WBERA", "500")))

    assert report.stablecoin_ratio == 0.5
    assert report.volatile_ratio == 0.5
    assert report.concentration_score == 100.0
    assert report.risk_level == "medium"


def test_two_splits_each_sum_to_one() -> None:
    """两套切分各自完整：稳定+波动 = 1，质押+可动用 = 1。

    这两组数回答的问题不同，界面上必须分组显示 —— 本文档就是那个口径的来源。
    """
    slices = (StakedSlice(symbol="sWBERA", underlying_symbol="WBERA", value_usd=Decimal("500")),)
    report = build_report(_wallet(("WBERA", "500")), slices)

    assert report.stablecoin_ratio + report.volatile_ratio == 1.0
    assert report.staking_ratio is not None
    assert report.liquid_ratio is not None
    assert report.staking_ratio + report.liquid_ratio == 1.0
    assert report.staking_ratio == 0.5
    # 质押那一行按**底层资产**分类（sWBERA 跟 WBERA），所以它进了波动比，没有被漏掉。
    assert report.volatile_ratio == 1.0


def test_unknown_staking_is_null_not_zero() -> None:
    """`staked=None` 是「没测到」→ 两个流动性比率给 null。

    给 0 会让界面显示「质押占比 0%」，读起来就是「确实没质押」，
    而事实是我们根本没去看。
    """
    report = build_report(_wallet(("WBERA", "1000")))

    assert report.staking_ratio is None
    assert report.liquid_ratio is None


def test_measured_zero_staking_is_zero() -> None:
    """空元组是「测过，确实没有质押」→ 0。与 None 是两件事，不能合并。"""
    report = build_report(_wallet(("WBERA", "1000")), ())

    assert report.staking_ratio == 0.0
    assert report.liquid_ratio == 1.0


def test_zero_value_holdings_are_ignored() -> None:
    """估值为 0 的条目不进组合，否则会给集中度塞进一堆无意义的项。"""
    report = build_report(_wallet(("WBERA", "0"), ("USDT0", "100")))

    assert report.stablecoin_ratio == 1.0
    assert report.volatile_ratio == 0.0
    assert report.top_asset == "USDT0"


def test_empty_wallet_is_unknown_not_low() -> None:
    """没有可估值的持仓 → unknown。报 low 等于替用户宣布「你很安全」，那是编的。"""
    report = build_report(_wallet())

    assert report.risk_level == "unknown"
    assert report.top_asset is None
    assert report.top_asset_ratio is None
