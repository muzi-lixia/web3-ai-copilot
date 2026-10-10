"""真实 PostgreSQL 回归。显式 RUN_DATABASE_TESTS=1 启用，所有测试使用随机钱包隔离。"""

import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from sqlalchemy import text

from app.agent.repository import ConversationRepository
from app.core.config import Settings
from app.core.exceptions import ConflictError, NonceInvalidError, NotFoundError, UnauthorizedError
from app.core.resources import BusinessResources
from app.foundation.auth import AuthService
from app.foundation.main import create_app
from app.foundation.results import ResultService
from app.infrastructure.database.session import Database

pytestmark = pytest.mark.skipif(os.getenv("RUN_DATABASE_TESTS") != "1", reason="需要显式启用本地 PostgreSQL")


@pytest.fixture
async def services():
    settings = Settings(_env_file=None)
    db = Database(settings.database_url)
    resources = BusinessResources(settings)
    value = SimpleNamespace(
        settings=settings,
        database=db,
        resources=resources,
        auth=AuthService(db, settings),
        results=ResultService(db, resources),
    )
    try:
        yield value
    finally:
        await db.close()


async def login(services):
    account = Account.create()
    challenge = await services.auth.challenge(account.address)
    signature = "0x" + account.sign_message(encode_defunct(text=challenge["message"])).signature.hex()
    result = await services.auth.login(challenge["message"], signature)
    return account, challenge, signature, result


async def test_restart_replay_rotation_and_logout(services):
    account, challenge, signature, credentials = await login(services)
    restarted = AuthService(services.database, services.settings)
    identity = await restarted.authenticate(credentials["token"])
    assert identity.address.lower() == account.address.lower()
    with pytest.raises(NonceInvalidError):
        await restarted.login(challenge["message"], signature)
    fresh = await restarted.refresh(credentials["refresh_token"])
    assert fresh["refresh_token"] != credentials["refresh_token"]
    with pytest.raises(UnauthorizedError):
        await restarted.refresh(credentials["refresh_token"])
    with pytest.raises(UnauthorizedError):
        await restarted.authenticate(fresh["token"])
    _, _, _, other = await login(services)
    current = await restarted.authenticate(other["token"])
    await restarted.logout(current)
    with pytest.raises(UnauthorizedError):
        await restarted.authenticate(other["token"])


async def test_stable_identity_and_rls_conversation_ownership(services):
    account, _, _, credentials = await login(services)
    repo = ConversationRepository(services.database, services.settings)
    user = credentials["user_id"]
    c = await repo.conversation(user)
    tid, created = await repo.create_turn(user, c["id"], str(uuid4()), "安全查询BERA")
    assert created
    assert (await repo.read(user, tid))["seq"] == 2
    with pytest.raises(NotFoundError):
        await repo.read(str(uuid4()), tid)
    with pytest.raises(ConflictError):
        await repo.create_turn(user, c["id"], str(uuid4()), "再次查询")
    await repo.update(user, tid, "查询完成", "completed", [])
    challenge = await services.auth.challenge(account.address)
    signature = "0x" + account.sign_message(encode_defunct(text=challenge["message"])).signature.hex()
    again = await services.auth.login(challenge["message"], signature)
    assert again["user_id"] == user
    assert (await repo.history(user))["items"][1]["content"] == "查询完成"
    await repo.clear(user)


async def test_result_ownership_and_cross_conversation_refs(services):
    _, _, _, credentials = await login(services)
    user = credentials["user_id"]
    payload = {
        "chains": [],
        "currency": "USD",
        "total_value": "0",
        "unpriced": [],
        "isComplete": True,
        "failed_chains": [],
        "scope": "registered_tokens",
        "queriedAt": "2026-10-10T10:00:00Z",
    }
    meta = await services.results.save(user, payload)
    assert "total_value" not in meta
    assert (await services.results.get(user, meta["result_id"]))["total_value"] == "0"
    with pytest.raises(NotFoundError):
        await services.results.get(str(uuid4()), meta["result_id"])
    repo = ConversationRepository(services.database, services.settings)
    c = await repo.conversation(user)
    await repo.add_ref(user, c["id"], meta["result_id"])
    await repo.check_refs(user, c["id"], [meta["result_id"]])
    with pytest.raises(NotFoundError):
        await repo.check_refs(user, str(uuid4()), [meta["result_id"]])


async def test_http_auth_and_trace_propagation(services):
    app = create_app(services)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/users/me")
        assert response.status_code == 401 and len(response.headers["x-trace-id"]) == 32
        response = await client.post("/api/v1/auth/challenges", json={"address": "invalid"})
        assert response.status_code == 422
        _, _, _, credentials = await login(services)
        headers = {"Authorization": "Bearer " + credentials["token"], "X-Trace-Id": "a" * 32}
        response = await client.get("/api/v1/auth/identity", headers=headers)
        assert response.status_code == 200 and "address" not in response.json()["data"]
        assert response.headers["x-trace-id"] == "a" * 32
        response = await client.delete("/api/v1/auth/session", headers=headers)
        assert response.status_code == 204
        assert (await client.get("/api/v1/users/me", headers=headers)).status_code == 401


async def test_full_agent_http_tool_checkpoint_and_history(services, monkeypatch):
    """真实框架执行与真实 PG，替换模型和 RPC，验证关键接缝而不依赖外网。"""
    import asyncio
    import json
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from app.agent.main import postgres_dsn
    from app.agent.privacy import normalize_query
    from app.agent.service import ChatService
    from app.infrastructure.database import runtime_lock
    from app.infrastructure.database.runtime_lock import RuntimeLock
    from app.infrastructure.foundation_client import FoundationClient
    from tests.factories import make_wallet
    from tests.unit.test_service_refactor import QueryModel

    _, _, _, credentials = await login(services)
    monkeypatch.setattr(
        "app.modules.asset.service.get_wallet_assets",
        AsyncMock(return_value=make_wallet(("BERA", "123456.789"))),
    )

    @asynccontextmanager
    async def controlled_model(settings):
        yield QueryModel()

    monkeypatch.setattr("app.agent.service.chat_model", controlled_model)
    foundation = FoundationClient(services.settings, transport=httpx.ASGITransport(app=create_app(services)))
    foundation.default_chain_id = 80094
    repository = ConversationRepository(services.database, services.settings)
    # 此测试验证执行/Checkpoint，不验证启动收尾。使用随机测试租约，
    # 不抢正在运行的真实服务锁，也不调用全局恢复函数影响真实用户轮次。
    monkeypatch.setattr(runtime_lock, "LOCK_KEY", 1 + uuid4().int % (2**31 - 2))
    async with AsyncPostgresSaver.from_conn_string(postgres_dsn(services.settings.database_url)) as saver:
        runtime = ChatService(
            repository, foundation, services.settings, RuntimeLock(services.database.engine), saver
        )
        await runtime.lease.acquire()
        try:
            response = await runtime.submit(
                credentials["user_id"],
                credentials["token"],
                str(uuid4()),
                normalize_query("查询我的BERA余额，我有123456.789个BERA"),
                display_text="查询我的BERA余额，我有123456.789个BERA",
            )
            task = runtime.tasks[response["turn_id"]]
            await asyncio.wait_for(task, 10)
            snapshot = await runtime.snapshot(credentials["user_id"], response["turn_id"])
            assert snapshot["status"] == "completed", snapshot["error"]
            assert snapshot["message"]["result_refs"]
            stored = await saver.aget_tuple({"configurable": {"thread_id": snapshot["session_id"]}})
            serialized = json.dumps(stored.checkpoint, default=str)
            assert "123456.789" not in serialized
            assert credentials["token"] not in serialized and credentials["address"] not in serialized
            history = await repository.history(credentials["user_id"])
            assert len(history["items"]) == 2
            assert history["items"][0]["content"] == "查询我的BERA余额，我有123456.789个BERA"
            await runtime.clear(credentials["user_id"])
            assert await saver.aget_tuple({"configurable": {"thread_id": snapshot["session_id"]}}) is None
        finally:
            await runtime.stop()
            await foundation.close()


async def test_separate_service_database_privileges(services):
    """开发初始化的两个角色也真实验证隔离，不能仅靠目录划分宣称数据隔离。"""
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import DBAPIError

    for role, password, forbidden_table in (
        ("app_agent", "copilot_dev_agent", "service_users"),
        ("app_foundation", "copilot_dev_foundation", "agent_conversations"),
    ):
        url = make_url(services.settings.database_url).set(username=role, password=password)
        db = Database(url.render_as_string(hide_password=False))
        try:
            with pytest.raises(DBAPIError):
                async with db.sessions() as session:
                    await session.execute(text("SELECT count(*) FROM " + forbidden_table))
        finally:
            await db.close()


async def test_original_display_persistence_idempotency_and_ownership(services):
    from app.agent.privacy import normalize_query

    _, _, _, credentials = await login(services)
    user = credentials["user_id"]
    repository = ConversationRepository(services.database, services.settings)
    conversation = await repository.conversation(user)
    original = "  查询我的BERA余额，我有98765.4321个BERA\n"
    safe = normalize_query(original).text
    client_id = str(uuid4())
    tid, created = await repository.create_turn(user, conversation["id"], client_id, safe, original)
    assert created
    stored = await repository.read(user, tid)
    assert stored["display_text"] == original
    assert "98765.4321" not in stored["query"]
    assert await repository.find_client_turn(user, conversation["id"], client_id, safe, original) == tid
    with pytest.raises(ConflictError):
        await repository.find_client_turn(user, conversation["id"], client_id, safe, "查询我的BERA余额")
    with pytest.raises(ConflictError):
        await repository.create_turn(user, conversation["id"], client_id, safe, "查询我的BERA余额")
    reloaded = ConversationRepository(services.database, services.settings)
    assert (await reloaded.history(user))["items"][0]["content"] == original
    _, _, _, another = await login(services)
    assert not (await reloaded.history(another["user_id"]))["items"]
    with pytest.raises(NotFoundError):
        await reloaded.read(another["user_id"], tid)
