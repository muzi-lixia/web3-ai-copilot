"""验证路由迁移后认证、错误响应和现有业务入口仍正确接线。"""

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from app.main import app


@pytest.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_wallet_login_and_nonce_replay(client):
    account = Account.create()
    response = await client.post("/api/v1/auth/nonce", json={"address": account.address})
    assert response.status_code == 200
    message = response.json()["message"]
    signature = account.sign_message(encode_defunct(text=message)).signature.hex()
    payload = {"address": account.address, "signature": signature}
    response = await client.post("/api/v1/auth/verify", json=payload)
    assert response.status_code == 200
    token = response.json()["token"]
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json()["address"].lower() == account.address.lower()
    replay = await client.post("/api/v1/auth/verify", json=payload)
    assert replay.status_code == 400
    assert replay.json()["error"]["code"] == "nonce_invalid"


@pytest.mark.parametrize("path", [
    "/api/v1/auth/me",
    "/api/v1/market/quotes",
    "/api/v1/wallet/0x0000000000000000000000000000000000000001/assets",
    "/api/v1/risk/0x0000000000000000000000000000000000000001/report",
    "/api/v1/staking/0x0000000000000000000000000000000000000001/positions",
])
async def test_business_routes_require_authentication(client, path):
    response = await client.get(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_health_and_validation_response(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    response = await client.post("/api/v1/auth/nonce", json={"address": "invalid"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
