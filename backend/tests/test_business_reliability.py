"""Regression cases for incomplete input, exact amounts and upstream failures."""

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal, getcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from requests.exceptions import Timeout
from web3.exceptions import ContractLogicError

from app.api.schemas.common import DataIssue
from app.api.schemas.market import TokenMarket
from app.config.constants.chains import BERACHAIN
from app.config.constants.staking import modules_for
from app.config.constants.tokens import tokens_for
from app.config.settings import settings
from app.infra.blockchain import chain, staking_vault
from app.infra.singleflight import singleflight
from app.services import asset_service, market_service, staking_service
from app.services.risk_service import build_report
from app.services.staking_service import StakedSlice
from app.services.units import to_human
from app.services.valuation import value_assets
from app.shared.errors import UpstreamError
from tests.test_risk_service import _wallet


def quote(symbol="WBERA"):
    return TokenMarket(
        symbol=symbol,
        price_usd=Decimal("1.234567890123456789"),
        source="defillama",
        updated_at=datetime.now(UTC),
    )


def test_uint256_conversion_and_valuation_are_exact():
    raw = 2**256 - 1
    amount = to_human(raw, 18)
    assert format(amount, "f").replace(".", "") == str(raw)
    assert to_human(0, 18) == Decimal(0)
    asset = _wallet(("WBERA", "1")).assets[0].model_copy(update={"amount": amount})
    price = quote()
    before = getcontext().prec
    result = value_assets([asset], {"WBERA": price})
    digits = str(raw * 1234567890123456789)
    expected = Decimal(digits[:-36] + "." + digits[-36:])
    assert result.total_value_usd == expected
    assert getcontext().prec == before
    assert result.assets[0].model_dump(mode="json")["amount"] == format(amount, "f")


def test_unread_balance_is_not_zero(monkeypatch):
    monkeypatch.setattr(chain, "read_native_balance", lambda *args: 0)
    monkeypatch.setattr(chain, "read_erc20_balances", lambda *args: {})
    assets = asset_service._read_holdings_on_node(
        SimpleNamespace(eth=SimpleNamespace(block_number=123)), BERACHAIN, "0x" + "1" * 40
    )
    assert all(a.amount is None for a in assets if a.kind == "erc20")
    result = value_assets(assets, {})
    assert not result.has_valuation
    assert all(a.value_usd is None for a in result.assets)


async def test_wallet_response_propagates_missing_balance(monkeypatch):
    asset = _wallet(("WBERA", "1")).assets[0].model_copy(update={"amount": None})
    monkeypatch.setattr(asset_service, "_read_holdings", lambda *args: [asset])
    monkeypatch.setattr(asset_service, "_load_quotes", AsyncMock(return_value={"WBERA": quote()}))
    result = await asset_service.get_wallet_assets(80094, "0x" + "1" * 40)
    assert result.status == "partial"
    assert result.issues[0].code == "balance_unavailable"
    assert result.assets[0].amount is None
    assert result.total_value_usd == 0


def test_missing_volatile_price_cannot_produce_low_risk():
    wallet = _wallet(("USDT0", "100"), ("BERA", "1000"))
    wallet.assets[1].value_usd = None
    wallet.missing_price = ["BERA"]
    report = build_report(wallet, ())
    assert report.risk_level == "unknown"
    assert report.status == "unavailable"
    assert report.stablecoin_ratio is None
    assert report.concentration_score is None
    assert report.issues


def test_unmeasured_staking_prevents_full_portfolio_conclusion():
    report = build_report(_wallet(("USDT0", "100")), None)
    assert report.risk_level == "unknown"
    assert report.staking_ratio is None


def test_wrapped_and_staked_bera_are_one_price_exposure():
    report = build_report(
        _wallet(("BERA", "100"), ("WBERA", "100")),
        (StakedSlice(symbol="sWBERA", underlying_symbol="WBERA", value_usd=Decimal(100)),),
    )
    assert report.concentration_score == 100
    assert report.staking_ratio == pytest.approx(1 / 3)


async def test_missing_staking_price_prevents_ratio(monkeypatch):
    built = staking_service._Positions(
        missing_price=["WBERA"], issues=[DataIssue(code="price_unavailable", message="缺价")]
    )
    monkeypatch.setattr(staking_service, "_build_positions", AsyncMock(return_value=built))
    monkeypatch.setattr(staking_service, "_load_liquid_value", AsyncMock(return_value=Decimal(100)))
    result = await staking_service.get_summary(80094, "0x" + "1" * 40)
    assert result.portfolio_ratio is None
    assert result.status == "partial"


async def test_missing_wallet_price_prevents_denominator(monkeypatch):
    wallet = _wallet(("WBERA", "0"))
    wallet.missing_price = ["WBERA"]
    monkeypatch.setattr(asset_service, "get_wallet_assets", AsyncMock(return_value=wallet))
    assert await staking_service._load_liquid_value(80094, wallet.address) is None


def test_failed_withdrawal_is_not_silently_discarded():
    with pytest.raises(UpstreamError, match="42"):
        staking_vault._request((False, b""), 42)
    # An explicitly empty on-chain request remains distinguishable from failure.
    assert staking_vault._request((True, b"\0" * (staking_vault._REQUEST_WORDS * 32)), 42) is None


async def test_risk_staking_read_skips_earnings(monkeypatch):
    module = modules_for(80094)[0]
    underlying = next(t for t in tokens_for(80094) if t.symbol == module.underlying_symbol)
    reading = staking_vault.VaultReading(
        shares=0,
        underlying=0,
        asset=underlying.address,
        total_assets=10**18,
        total_supply=10**18,
        cooldown_seconds=604800,
        pending=(),
    )
    monkeypatch.setattr(staking_service, "_load_readings", AsyncMock(return_value={module.key: reading}))
    monkeypatch.setattr(staking_service, "_load_quotes", AsyncMock(return_value={}))
    apy = AsyncMock(side_effect=AssertionError("unused APY requested"))
    earnings = AsyncMock(side_effect=AssertionError("unused earnings requested"))
    monkeypatch.setattr(staking_service.beep, "get_stake_apy_safe", apy)
    monkeypatch.setattr(staking_service.beep, "gather_earnings", earnings)
    result = await staking_service.get_staked_slices(80094, "0x" + "1" * 40)
    assert result is not None
    apy.assert_not_awaited()
    earnings.assert_not_awaited()


async def test_partial_module_failure_blocks_risk_staking(monkeypatch):
    monkeypatch.setattr(staking_service, "_load_readings", AsyncMock(return_value={}))
    monkeypatch.setattr(staking_service, "_load_quotes", AsyncMock(return_value={}))
    assert await staking_service.get_staked_slices(80094, "0x" + "1" * 40) is None


async def test_quote_token_never_inherits_base_metrics():
    base, target = "0x" + "1" * 40, "0x" + "2" * 40
    payload = [
        {
            "baseToken": {"address": base},
            "quoteToken": {"address": target},
            "priceUsd": "10",
            "priceNative": "2",
            "priceChange": {"h24": 50},
            "marketCap": "1000000",
            "volume": {"h24": 100},
            "liquidity": {"usd": 1000},
        }
    ]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await market_service._from_dexscreener(client, BERACHAIN, (base, target))
    assert result[target].price_usd == Decimal(5)
    assert result[target].market_cap is None
    assert result[target].change_24h is None
    assert result[target].volume_24h is None
    assert result[base].change_24h == 50


def test_fdv_is_not_market_cap_and_nonfinite_is_rejected():
    assert market_service._quote_from_pair({"fdv": "1000"}, Decimal(1)).market_cap is None
    for value in ("NaN", "Infinity", "-1"):
        assert market_service._decimal(value) is None
    assert market_service._float_or_none("NaN") is None


async def test_stale_cache_has_a_hard_expiry(monkeypatch):
    monkeypatch.setattr(
        market_service,
        "_cache",
        {80094: (time.monotonic() - settings.market_max_stale_seconds - 1, {"WBERA": quote()})},
    )
    monkeypatch.setattr(market_service, "_fetch", AsyncMock(side_effect=UpstreamError("offline")))
    with pytest.raises(UpstreamError):
        await market_service._load(BERACHAIN)


async def test_recent_stale_cache_remains_explicit(monkeypatch):
    monkeypatch.setattr(market_service, "_cache", {80094: (time.monotonic() - 1, {"WBERA": quote()})})
    monkeypatch.setattr(market_service, "_fetch", AsyncMock(side_effect=UpstreamError("offline")))
    result = await market_service._load(BERACHAIN)
    assert result["WBERA"].stale


def test_rpc_failover_retries_transport_but_not_contract_errors(monkeypatch):
    monkeypatch.setattr(chain, "_candidate_urls", lambda _: ["https://a/secret", "https://b/secret"])
    monkeypatch.setattr(chain, "_good_rpc", {})
    factory = Mock(return_value=SimpleNamespace(eth=SimpleNamespace(chain_id=80094)))
    factory.HTTPProvider = Mock()
    monkeypatch.setattr(chain, "Web3", factory)
    operation = Mock(side_effect=[Timeout("https://a/secret"), 7])
    assert chain.read_with_failover(BERACHAIN, operation) == 7
    assert operation.call_count == 2
    operation = Mock(side_effect=ContractLogicError("private response"))
    with pytest.raises(UpstreamError) as error:
        chain.read_with_failover(BERACHAIN, operation)
    assert "private" not in str(error.value)
    assert operation.call_count == 1


def test_rpc_credentials_not_exposed(monkeypatch, caplog):
    secret = "DO_NOT_EXPOSE_THIS_KEY"
    monkeypatch.setattr(chain, "_candidate_urls", lambda _: [f"https://user:pass@rpc/{secret}"])
    factory = Mock(side_effect=Timeout(secret))
    factory.HTTPProvider = Mock()
    monkeypatch.setattr(chain, "Web3", factory)
    for fn in (lambda: chain.connect(BERACHAIN), lambda: chain.read_with_failover(BERACHAIN, lambda _: 1)):
        with pytest.raises(UpstreamError) as error:
            fn()
        assert secret not in str(error.value)
    assert secret not in caplog.text


async def test_singleflight_survives_one_caller_cancellation():
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    @singleflight
    async def load(key):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return key

    first = asyncio.create_task(load("wallet"))
    await started.wait()
    second = asyncio.create_task(load("wallet"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == "wallet"
    assert calls == 1
    assert await load("wallet") == "wallet"
    assert calls == 2  # no persistent cache


async def test_old_observation_cannot_be_refreshed_by_cache_timestamp(monkeypatch):
    from datetime import timedelta

    old = quote().model_copy(
        update={
            "updated_at": datetime.now(UTC)
            - timedelta(seconds=settings.market_cache_ttl + settings.market_max_stale_seconds + 1)
        }
    )
    monkeypatch.setattr(market_service, "_cache", {80094: (time.monotonic() + 60, {"WBERA": old})})
    assert await market_service._load(BERACHAIN) == {}


def test_vault_calls_use_same_block_and_keep_pending_value():
    from web3 import Web3

    module = modules_for(80094)[0]
    owner = "0x" + "1" * 40
    underlying = next(t for t in tokens_for(80094) if t.symbol == module.underlying_symbol)
    codec = Web3().codec

    def word(value):
        return True, codec.encode(["uint256"], [value])

    first = [
        word(10**18),
        word(2 * 10**18),
        word(10**18),
        (True, codec.encode(["address"], [underlying.address])),
        word(604800),
        (True, codec.encode(["uint256[]"], [[42]])),
    ]
    second = [
        word(2 * 10**18),
        (
            True,
            codec.encode(
                ["uint256", "uint256", "uint256", "address", "address"],
                [10**18, 10**18, 100, owner, owner],
            ),
        ),
    ]
    call = Mock(side_effect=[first, second])
    contract = SimpleNamespace(
        functions=SimpleNamespace(aggregate3=Mock(return_value=SimpleNamespace(call=call)))
    )
    w3 = SimpleNamespace(
        codec=codec, eth=SimpleNamespace(block_number=123, contract=Mock(return_value=contract))
    )
    result = staking_vault.read_vault(w3, BERACHAIN, module, owner)
    assert call.call_count == 2
    assert all(c.kwargs == {"block_identifier": 123} for c in call.call_args_list)
    assert result.underlying == 2 * 10**18
    assert result.pending[0].request_id == 42
    assert result.pending[0].assets == 10**18
