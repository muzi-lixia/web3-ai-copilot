"""验证路由迁移后认证、错误响应和现有业务入口仍正确接线。"""

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from app.main import app


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_wallet_login_and_nonce_replay(client):
    account = Account.create()
    response = await client.post("/api/v1/auth/challenges", json={"address": account.address})
    assert response.status_code == 201
    message = response.json()["data"]["message"]
    signature = account.sign_message(encode_defunct(text=message)).signature.hex()
    payload = {"address": account.address, "signature": signature}
    response = await client.post("/api/v1/auth/tokens", json=payload)
    assert response.status_code == 200
    token = response.json()["data"]["token"]
    response = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["data"]["address"].lower() == account.address.lower()
    replay = await client.post("/api/v1/auth/tokens", json=payload)
    assert replay.status_code == 400
    assert replay.json()["code"] == 40003


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/users/me",
        "/api/v1/markets/quotes",
        "/api/v1/wallets/0x0000000000000000000000000000000000000001/assets",
        "/api/v1/wallets/0x0000000000000000000000000000000000000001/risk-report",
        "/api/v1/wallets/0x0000000000000000000000000000000000000001/staking-positions",
    ],
)
async def test_business_routes_require_authentication(client, path):
    response = await client.get(path)
    assert response.status_code == 401
    assert response.json()["code"] == 40101


async def test_health_and_validation_response(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ok"
    response = await client.post("/api/v1/auth/challenges", json={"address": "invalid"})
    assert response.status_code == 422
    assert response.json()["code"] == 42201
