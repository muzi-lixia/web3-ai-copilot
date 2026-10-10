"""应用配置与缓存隔离，覆盖 HTTP、行情和 RPC 的实际资源传递。"""

import time
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from app.core.config import Settings
from app.core.resources import BusinessResources
from app.infrastructure.blockchain.chains import BERACHAIN
from app.infrastructure.blockchain.client import _candidate_urls
from app.main import create_app
from app.modules.market import service as market
from app.modules.market.schemas import TokenMarket, TokenMarketList


def test_rpc_overrides_and_cache_belong_to_resources():
    a = BusinessResources(Settings(_env_file=None, rpc_overrides="80094=https://rpc-a.example"))
    b = BusinessResources(Settings(_env_file=None, rpc_overrides="80094=https://rpc-b.example"))
    a.good_rpc[80094] = "https://rpc-a.example"
    assert _candidate_urls(BERACHAIN, resources=a) == ["https://rpc-a.example"]
    assert _candidate_urls(BERACHAIN, resources=b) == ["https://rpc-b.example"]
    assert b.good_rpc == {} and a.market_cache is not b.market_cache


async def test_market_cache_is_not_shared_between_applications(monkeypatch):
    a, b = BusinessResources(Settings(_env_file=None)), BusinessResources(Settings(_env_file=None))
    quote = TokenMarket(
        symbol="BERA", price_usd=Decimal("1"), source="dexscreener", updated_at=datetime.now(UTC)
    )
    a.market_cache[80094] = (time.monotonic() + 60, {"BERA": quote})
    fetch = AsyncMock(return_value={"BERA": quote.model_copy(update={"price_usd": Decimal("2")})})
    monkeypatch.setattr(market, "_fetch", fetch)
    assert (await market._load(BERACHAIN, resources=a))["BERA"].price_usd == 1
    assert (await market._load(BERACHAIN, resources=b))["BERA"].price_usd == 2
    fetch.assert_awaited_once_with(BERACHAIN, resources=b)


async def test_business_http_uses_container_default_chain_and_resources(monkeypatch):
    instances = []
    for chain in (80094, 1):
        settings = Settings(_env_file=None, default_chain_id=chain)
        from app.infrastructure.database.session import Database

        instances.append(
            SimpleNamespace(
                settings=settings,
                database=Database(settings.database_url),
                resources=BusinessResources(settings),
            )
        )
    seen = []

    async def query(chain_id, symbols=None, *, resources=None):
        seen.append((chain_id, resources))
        return TokenMarketList(chain_id=chain_id, chain_name="test", tokens=[])

    monkeypatch.setattr(market, "get_quotes", query)
    try:
        for services in instances:
            owner = "0x" + "11" * 20
            services.auth = SimpleNamespace(
                authenticate=AsyncMock(return_value=SimpleNamespace(address=owner, user_id="test-user")),
                limit=AsyncMock(),
                settings=services.settings,
            )
            token = "test"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(services)), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/api/v1/markets/quotes", headers={"Authorization": f"Bearer {token}"}
                )
                assert response.status_code == 200
                assert response.json()["data"]["chain_id"] == services.settings.default_chain_id
        assert seen == [(s.settings.default_chain_id, s.resources) for s in instances]
    finally:
        for services in instances:
            await services.database.close()
