"""跨测试共享的数据工厂，不让测试案例相互导入。"""

from datetime import UTC, datetime
from decimal import Decimal

from app.modules.asset.schemas import Asset, WalletAssets

_NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def make_wallet(*holdings: tuple[str, str]) -> WalletAssets:
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
