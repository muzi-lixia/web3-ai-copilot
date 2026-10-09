"""资源接口契约回归：真实路由、响应表示、认证挑战及旧接口移除。"""

from uuid import uuid4

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from app.main import create_app


@pytest.fixture
async def client():
    """独立应用状态防止挑战与限流污染其他测试，不启动真实数据库。"""
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    await app.state.services.close()


async def test_challenge_location_representation_and_consumption(client):
    """201 的 Location 可读取且不消费挑战；签名成功后原挑战变为 404。"""
    account = Account.create()
    created = await client.post("/api/v1/auth/challenges", json={"address": account.address})
    assert created.status_code == 201
    assert set(created.json()) == {"code", "msg", "data"}
    location = created.headers["Location"]
    assert created.json()["code"] == 0 and created.json()["msg"] == "success"
    read = await client.get(location)
    assert read.status_code == 200
    assert read.json()["data"]["message"] == created.json()["data"]["message"]
    signature = account.sign_message(encode_defunct(text=read.json()["data"]["message"])).signature.hex()
    issued = await client.post(
        "/api/v1/auth/tokens", json={"address": account.address, "signature": signature}
    )
    assert issued.status_code == 200 and issued.json()["data"]["token_type"] == "Bearer"
    assert issued.headers["Cache-Control"] == "no-store"
    assert (await client.get(location)).status_code == 404
    user = await client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {issued.json()['data']['token']}"}
    )
    assert user.json()["data"]["address"].lower() == account.address.lower()


async def test_error_envelope_and_standard_headers(client):
    """认证、校验、路由与方法错误采用相同媒体类型，并保留标准头。"""
    unauthorized = await client.get("/api/v1/users/me")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["WWW-Authenticate"] == "Bearer"
    assert unauthorized.headers["Content-Type"].startswith("application/json")
    assert set(unauthorized.json()) == {"code", "msg", "data"}
    invalid = await client.post("/api/v1/auth/challenges", json={"address": "wrong", "token": "secret"})
    assert invalid.status_code == 422 and invalid.json()["data"]["errors"]
    assert "secret" not in invalid.text
    missing = await client.get("/api/v1/does-not-exist")
    assert missing.status_code == 404 and missing.json()["code"] == 40401
    wrong_method = await client.post("/api/v1/chat/session")
    assert wrong_method.status_code == 405
    assert "PUT" in wrong_method.headers["Allow"]
    assert wrong_method.json()["code"] == 40501


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/nonce",
        "/api/v1/auth/verify",
        "/api/v1/auth/me",
        "/api/v1/wallet/0x0000000000000000000000000000000000000001/assets",
        "/api/v1/risk/0x0000000000000000000000000000000000000001/report",
        "/api/v1/staking/0x0000000000000000000000000000000000000001/positions",
        "/api/v1/market/quotes",
        f"/api/v1/chat/turns/{uuid4()}/cancel",
        f"/api/v1/chat/turns/{uuid4()}/retry",
    ],
)
async def test_old_paths_are_not_registered(client, path):
    """删除旧接口而非提供兼容别名，任何方法都不能继续调用旧业务入口。"""
    response = await client.post(path)
    assert response.status_code == 404


async def test_openapi_matches_error_envelope_and_bearer_security(client):
    """OpenAPI 的认证与错误表示必须匹配实际响应，不保留旧路径或默认错误数组。"""
    schema = (await client.get("/api/v1/openapi.json")).json()
    route = schema["paths"]["/api/v1/chat/session/turns"]["post"]
    assert route["security"] == [{"WalletBearer": []}]
    assert "application/json" in route["responses"]["422"]["content"]
    assert "post" not in schema["paths"]["/api/v1/chat/session"]
    assert "/api/v1/auth/verify" not in schema["paths"]


async def test_invalid_challenge_identifier_is_validation_error(client):
    """非法 Unicode 挑战 ID 不进入字符串恒定时比较，避免触发内部 500。"""
    response = await client.get("/api/v1/auth/challenges/中文")
    assert response.status_code == 422 and response.json()["code"] == 42201
