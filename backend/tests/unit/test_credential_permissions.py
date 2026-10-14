"""使用真实路由与鉴权依赖验证 Agent 查询白名单，禁止管理与会话操作。"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from fastapi import FastAPI

from app.agent.api import router as agent_router
from app.core.exceptions import UnauthorizedError
from app.core.http.errors import register_exception_handlers
from app.foundation.config import FoundationSettings
from app.foundation.main import create_app
from app.modules.identity.service import AuthService, Identity

ADDRESS = "0x" + "11" * 20


def services_for(scope):
    settings = FoundationSettings(_env_file=None, admin_addresses=ADDRESS)
    auth = SimpleNamespace(
        settings=settings,
        authenticate=AsyncMock(return_value=Identity("u", "s", ADDRESS, scope)),
        limit=AsyncMock(),
        logout=AsyncMock(),
        issue_agent_permit=AuthService(SimpleNamespace(), settings).issue_agent_permit,
    )
    results = SimpleNamespace(
        **{name: AsyncMock(return_value={}) for name in ("query", "aggregate", "read", "delete")}
    )
    knowledge = SimpleNamespace(
        **{
            name: AsyncMock(return_value={})
            for name in ("upload", "listing", "read", "retry", "remove", "search", "reindex")
        }
    )
    return SimpleNamespace(
        settings=settings,
        auth=auth,
        results=results,
        knowledge=knowledge,
        resources=SimpleNamespace(settings=settings),
    )


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("DELETE", "/auth/session", None),
        ("GET", "/users/me", None),
        ("POST", "/auth/agent-permits", None),
        ("DELETE", "/me/balance-results", {"result_ids": ["r"]}),
        ("GET", "/me/balance-results/r", None),
        ("POST", "/tokens/resolutions", {"chain_id": 1, "contract": ADDRESS}),
        ("GET", "/me/assets", None),
        ("GET", "/me/risk-report", None),
        ("GET", "/me/staking-positions", None),
        ("GET", "/knowledge/documents", None),
        ("GET", "/knowledge/status", None),
        ("GET", "/knowledge/documents/d", None),
        ("POST", "/knowledge/documents/d/retry", None),
        ("POST", "/knowledge/documents/d/reindex", None),
        ("DELETE", "/knowledge/documents/d", None),
        ("POST", "/knowledge/documents", None),
    ],
)
async def test_agent_admin_cannot_access_user_or_admin_routes(method, path, body):
    services = services_for("agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)), base_url="http://test"
    ) as client:
        kwargs = {"json": body} if body else {}
        if path == "/knowledge/documents" and method == "POST":
            kwargs = {"files": {"file": ("test.txt", b"test", "text/plain")}}
        response = await client.request(
            method, "/api/v1" + path, headers={"Authorization": "Bearer permit"}, **kwargs
        )
    assert response.status_code == 403, response.text
    for service in (services.results, services.knowledge):
        for mock in vars(service).values():
            mock.assert_not_awaited()
    services.auth.logout.assert_not_awaited()
    services.auth.limit.assert_not_awaited()


@pytest.mark.parametrize("scope", ["user", "agent"])
@pytest.mark.parametrize(
    "path,body",
    [
        ("/me/balance-results", {}),
        ("/me/aggregations", {"result_ids": ["r"]}),
        ("/knowledge/search", {"query": "质押规则"}),
    ],
)
async def test_query_routes_accept_both_credential_types(scope, path, body):
    services = services_for(scope)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)), base_url="http://test"
    ) as client:
        response = await client.post("/api/v1" + path, json=body, headers={"Authorization": "Bearer token"})
    assert response.status_code in (200, 201), response.text


@pytest.mark.parametrize("scope", ["agent", "unknown", None])
async def test_chat_rejects_non_user_identity_before_accessing_history(scope):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(agent_router, prefix="/api/v1")
    repository = SimpleNamespace(history=AsyncMock())
    app.state.services = SimpleNamespace(
        client=SimpleNamespace(identity=AsyncMock(return_value={"user_id": "u", "scope": scope})),
        repository=repository,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/chat/session/messages", headers={"Authorization": "Bearer permit"}
        )
    assert response.status_code == 403
    repository.history.assert_not_awaited()


async def test_unknown_signed_scope_rejected_before_database_access():
    settings = FoundationSettings(_env_file=None)
    token = jwt.encode(
        dict(
            sub="u", sid="s", address=ADDRESS, scope="admin", iat=int(time.time()), exp=int(time.time()) + 60
        ),
        settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(UnauthorizedError, match="用途无效"):
        await AuthService(SimpleNamespace(), settings).authenticate(token)


@pytest.mark.parametrize("scope", ["user", "agent"])
async def test_identity_projection_includes_scope_without_wallet(scope):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services_for(scope))), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/auth/identity", headers={"Authorization": "Bearer token"})
    assert response.status_code == 200
    assert response.json()["data"] == {"user_id": "u", "session_id": "s", "scope": scope}


async def test_user_admin_retains_management_and_permit_access():
    services = services_for("user")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)), base_url="http://test"
    ) as client:
        headers = {"Authorization": "Bearer token"}
        for method, path in [
            ("GET", "/knowledge/documents"),
            ("DELETE", "/knowledge/documents/d"),
            ("POST", "/auth/agent-permits"),
            ("DELETE", "/auth/session"),
        ]:
            response = await client.request(method, "/api/v1" + path, headers=headers)
            assert response.status_code in (200, 204), response.text
    services.knowledge.listing.assert_awaited_once()
    services.knowledge.remove.assert_awaited_once_with("d")
    services.auth.logout.assert_awaited_once()


async def test_agent_permit_still_requires_active_login_session():
    from contextlib import asynccontextmanager

    settings = FoundationSettings(_env_file=None)
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(first=lambda: None)))

    @asynccontextmanager
    async def sessions():
        yield session

    auth = AuthService(SimpleNamespace(sessions=sessions), settings)
    token = auth.issue_agent_permit(Identity("u", "s", ADDRESS))
    with pytest.raises(UnauthorizedError, match="登录会话已失效"):
        await auth.authenticate(token)
    session.execute.assert_awaited_once()
