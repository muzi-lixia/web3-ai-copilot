"""真实 PostgreSQL 集成测试：验证 RLS、事务、幂等、恢复和摘要。

RUN_CHAT_DB_TESTS=1 才启用，DATABASE_URL 应指向已初始化的专用测试库。
测试使用随机钱包地址，结束时仅删除该身份的会话；不会用 SQLite 模拟 RLS 行为。"""

import asyncio
import json
import os
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.ai.conversation.models import ChatMessage, ChatSession, ChatSummary, ChatTurn
from app.ai.conversation.service import ChatRuntime
from app.ai.memory import service as context
from app.core.config import settings
from app.core.container import ApplicationServices
from app.core.exceptions import ChatUnavailableError, ConflictError
from app.infrastructure.database.runtime_lock import RuntimeLock
from app.main import create_app
from app.modules.auth.service import AuthService

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_CHAT_DB_TESTS") != "1", reason="Requires real PostgreSQL test DB"
)


@pytest.fixture
async def services():
    services = ApplicationServices.build(settings)
    await services.chat.start()
    try:
        yield services
    finally:
        await services.close()


@pytest.fixture
async def owner(services):
    owner = "0x" + uuid4().hex + "12345678"
    yield owner
    await services.chat.stop()
    await services.repository.delete_all(owner)


@pytest.fixture
def repo(services):
    return services.repository


@pytest.fixture
def runtime(services):
    return services.chat


@pytest.fixture
def memory(services):
    return services.memory


@pytest.fixture
def ollama(services):
    return services.model


@pytest.fixture
def engine(services):
    return services.database.engine


@pytest.fixture
def SessionFactory(services):
    return services.database.sessions


@pytest.fixture
def transaction(services):
    return services.database.transaction


@pytest.fixture
async def client(services):
    app = create_app(services)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


def fresh_runtime(services):
    return ChatRuntime(
        services.repository, services.memory, services.model, RuntimeLock(services.database.engine), settings
    )


def headers(owner):
    """签发测试 JWT，模拟真实路由鉴权，避免通过覆盖鉴权依赖绕过身份链路。"""
    token, _ = AuthService(settings).create_token(owner)
    return {"Authorization": f"Bearer {token}"}


async def fixture_session(owner, transaction):
    """测试独立建立会话，包含升级前多会话数据；生产只提供 get_or_create_session。"""
    async with transaction(owner) as db:
        row = ChatSession(user_address=owner.lower())
        db.add(row)
        await db.flush()
        return {"id": str(row.id)}


async def fixture_turn(owner, session_id, question="我叫小林", answer="你好，小林", *, repo):
    """直接建立一个成功轮次作为历史夹具，不调用模型；消息和状态仍按真实事务保存。"""
    turn_id, _ = await repo.create_turn(owner, session_id, uuid4(), question)
    await repo.checkpoint(owner, turn_id, answer, "completed")
    return turn_id


async def test_private_history_rls_and_restart(owner, client, SessionFactory, engine, repo, transaction):
    """验证跨用户资源查询返回 404、未注入身份查询零行、连接池重建后历史仍存在。"""
    session = await fixture_session(owner, transaction)
    session_id = UUID(session["id"])
    turn_id = await fixture_turn(owner, session_id, repo=repo)
    other = "0x" + uuid4().hex + "12345678"
    for path in [
        f"/chat/turns/{turn_id}",
        f"/chat/turns/{turn_id}/events",
    ]:
        response = await client.get("/api/v1" + path, headers=headers(other))
        assert response.status_code == 404
    async with SessionFactory.begin() as db:
        # 未注入身份时，故意省略 WHERE 的查询也必须看不到任何私有行。
        assert (await db.scalar(select(func.count()).select_from(ChatSession))) == 0
    await engine.dispose()
    page = await repo.messages(owner, session_id)
    assert [m["content"] for m in page["items"]] == ["我叫小林", "你好，小林"]
    response = await client.put("/api/v1/chat/session")
    assert response.status_code == 401
    response = await client.post(
        "/api/v1/chat/session/turns",
        headers=headers(owner),
        json={"message": "你好", "client_msg_id": str(uuid4()), "history": []},
    )
    assert response.status_code == 422  # 客户端不能覆盖服务器管理的历史。


async def test_foreign_keys_reject_inconsistent_owner(owner, transaction):
    """即使绕过 Repository 直接插 ORM，对不上用户归属的关联也必须被数据库拒绝。"""
    session_id = UUID((await fixture_session(owner, transaction))["id"])
    other = "0x" + uuid4().hex + "12345678"
    with pytest.raises(IntegrityError):
        async with transaction(other) as db:
            db.add(
                ChatTurn(
                    session_id=session_id,
                    user_address=other,
                    client_msg_id=uuid4(),
                    prompt_message_id=uuid4(),
                    model="test",
                )
            )


async def test_idempotent_submit_busy_cancel_retry_and_delete(
    owner, monkeypatch, ollama, repo, runtime, transaction
):
    """验证重发不重复、并发新问题冲突、断开不取消、显式取消、重试复用问题及删除级联。"""
    session_id = UUID((await fixture_session(owner, transaction))["id"])
    seen = []
    delta_written = asyncio.Event()

    async def source(messages):
        seen.append(messages)
        yield {"type": "delta", "text": "半段回答"}
        await asyncio.sleep(settings.copilot_snapshot_seconds + 0.02)
        yield {"type": "delta", "text": "，继续"}
        delta_written.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ollama, "stream_chat", source)
    monkeypatch.setattr(settings, "copilot_snapshot_seconds", 0.01)
    client_id = uuid4()
    result = await runtime.submit_message(owner, client_id, "你好")
    turn_id = UUID(result["turn_id"])
    repeated = await runtime.submit_message(owner, client_id, "你好")
    assert repeated["turn_id"] == str(turn_id) and repeated["created"] is False
    with pytest.raises(ConflictError):
        await runtime.submit_message(owner, uuid4(), "第二个问题")
    await asyncio.wait_for(delta_written.wait(), 2)
    stream = runtime.subscribe(owner, turn_id)
    snapshot = await anext(stream)
    assert snapshot["message"]["content"] == "半段回答，继续"
    await stream.aclose()  # 关闭订阅不会停止独立生成任务。
    assert (await repo.turn_snapshot(owner, turn_id))["status"] == "running"
    final = await runtime.cancel(owner, turn_id)
    assert final["status"] == "cancelled"
    assert final["message"]["content"] == "半段回答，继续"
    retry = await runtime.retry(owner, turn_id, uuid4())
    await asyncio.sleep(0.02)
    page = await repo.messages(owner, session_id)
    assert len([m for m in page["items"] if m["role"] == "user"]) == 1
    assert UUID(retry["turn_id"]) != turn_id
    await runtime.delete_all(owner)
    async with transaction(owner) as db:
        for table in (ChatSession, ChatTurn, ChatMessage, ChatSummary):
            assert await db.scalar(select(func.count()).select_from(table)) == 0
    assert not runtime.running


async def test_summary_coverage_rebuild_and_failure_preserve_history(
    owner, monkeypatch, memory, ollama, repo, transaction
):
    """验证覆盖范围准确、周期重建从原文开始、失败不推进摘要版本且全部原文仍保留。"""
    session_id = UUID((await fixture_session(owner, transaction))["id"])
    for i in range(8):
        await fixture_turn(owner, session_id, f"第{i}轮，我叫小林。" * 20, "你好。" * 20, repo=repo)
    calls = []

    async def summarize(messages):
        body = json.loads(messages[-1]["content"])
        calls.append(body)
        value = {key: [] for key in context.KEYS}
        value["facts"] = [{"text": "用户叫小林", "sources": [body["messages"][0]["id"]]}]
        return value

    monkeypatch.setattr(ollama, "summarize", summarize)
    monkeypatch.setattr(settings, "copilot_recent_turns", 2)
    monkeypatch.setattr(settings, "copilot_summary_trigger", 200)
    monkeypatch.setattr(settings, "copilot_summary_rebuild_every", 1)
    summary = await memory.compact(owner, session_id)
    assert summary.version == 1 and summary.covered_through_seq == 12
    messages, version, info = await memory.build_context(owner, session_id, "我叫什么？")
    assert version == 1 and "用户叫小林" in str(messages)
    assert len(info["source_message_ids"]) == 4
    assert (await repo.messages(owner, session_id))["items"][0]["content"].startswith("第0轮")
    await fixture_turn(owner, session_id, repo=repo)
    summary = await memory.compact(owner, session_id, force=True)
    assert summary.version == 2
    # 周期重建必须重新读取原文，不能只继续压缩旧摘要。
    assert calls[-1]["messages"][0]["content"].startswith("第0轮")
    await fixture_turn(owner, session_id, repo=repo)

    async def failing(messages):
        raise ValueError("bad summary")

    monkeypatch.setattr(ollama, "summarize", failing)
    _, version, info = await memory.build_context(owner, session_id, "你好")
    assert version == 2 and "summary_failed" in info["issues"]
    assert len((await repo.messages(owner, session_id))["items"]) == 20


async def test_startup_recovers_orphans_and_rejects_second_worker(
    owner, repo, runtime, services, transaction
):
    """遗留运行即使没有超时也应收尾；第二个 worker 不能取得单实例运行锁。"""
    session_id = UUID((await fixture_session(owner, transaction))["id"])
    turn_id, _ = await repo.create_turn(owner, session_id, uuid4(), "你好")
    await repo.checkpoint(owner, turn_id, "进程崩溃前的快照")
    await runtime.stop()
    await runtime.start()
    snapshot = await repo.turn_snapshot(owner, turn_id)
    assert snapshot["status"] == "interrupted"
    assert snapshot["message"]["content"] == "进程崩溃前的快照"
    another = fresh_runtime(services)
    with pytest.raises(RuntimeError, match="worker"):
        await another.start()
    assert (await repo.messages(owner, session_id))["active_turn_id"] is None


async def test_complete_sse_terminal_snapshot_and_context(
    owner, client, monkeypatch, ollama, repo, transaction
):
    """验证 SSE 最终快照与数据库状态一致、上下文来自历史，公开响应不包含完整模型输入。"""
    captured = []

    async def source(messages):
        captured.append(messages)
        yield {"type": "delta", "text": "你好，小林"}
        yield {"type": "done", "partial": False, "usage": {"prompt_tokens": 20, "completion_tokens": 5}}

    monkeypatch.setattr(ollama, "stream_chat", source)
    session_id = UUID((await fixture_session(owner, transaction))["id"])
    await fixture_turn(owner, session_id, repo=repo)
    result = await client.post(
        "/api/v1/chat/session/turns",
        headers=headers(owner),
        json={"message": "我叫什么？", "client_msg_id": str(uuid4())},
    )
    assert result.status_code == 202
    response = await client.get(
        f"/api/v1/chat/turns/{result.json()['data']['turn_id']}/events", headers=headers(owner)
    )
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[-1]["type"] == "done" and events[-1]["status"] == "completed"
    assert events[-1]["message"]["content"] == "你好，小林"
    assert "model_messages" not in events[-1]["context_info"]
    assert [m["content"] for m in captured[0]][-3:] == ["我叫小林", "你好，小林", "我叫什么？"]


async def test_message_pagination_and_idempotency_payload(owner, repo, transaction):
    """同一幂等 ID 不能用于不同问题，消息翻页不能漏或重复。"""
    session_id = UUID((await fixture_session(owner, transaction))["id"])
    client_id = uuid4()
    turn_id, _ = await repo.create_turn(owner, session_id, client_id, "问题一")
    with pytest.raises(ConflictError, match="其他问题"):
        await repo.create_turn(owner, session_id, client_id, "问题二")
    await repo.checkpoint(owner, turn_id, "回答一", "completed")
    for i in range(3):
        await fixture_turn(owner, session_id, f"问题{i}", f"回答{i}", repo=repo)
    latest = await repo.messages(owner, session_id, limit=3)
    earlier = await repo.messages(owner, session_id, latest["next_cursor"], limit=100)
    assert len(latest["items"]) + len(earlier["items"]) == 8
    assert set(m["id"] for m in latest["items"]).isdisjoint(m["id"] for m in earlier["items"])


async def test_cancel_before_worker_starts_is_terminal(owner, repo, runtime, transaction):
    """运行已入库但任务尚未开始时，取消仍必须解除 running 状态，避免会话永久忙碌。"""
    session_id = UUID((await fixture_session(owner, transaction))["id"])
    turn_id, _ = await repo.create_turn(owner, session_id, uuid4(), "你好")
    # 模拟事务已提交但任务尚未登记的窗口，不依赖进程内 worker 存在。
    result = await runtime.cancel(owner, turn_id)
    assert result["status"] == "cancelled"
    assert (await repo.messages(owner, session_id))["active_turn_id"] is None


async def test_request_time_compaction_failure_still_generates(
    owner, client, monkeypatch, ollama, repo, runtime, transaction
):
    """摘要失败时完整轮次裁剪兜底仍能回答，同时保留完整历史和可观察的降级原因。"""
    session_id = UUID((await fixture_session(owner, transaction))["id"])
    for i in range(4):
        await fixture_turn(owner, session_id, f"问题{i}" * 100, "回答" * 100, repo=repo)

    async def broken_summary(messages):
        raise ValueError("invalid model summary")

    async def source(messages):
        yield {"type": "delta", "text": "仍可正常对话"}
        yield {"type": "done", "partial": False}

    monkeypatch.setattr(settings, "copilot_recent_turns", 1)
    monkeypatch.setattr(settings, "copilot_summary_trigger", 200)
    monkeypatch.setattr(settings, "copilot_input_budget", 700)
    monkeypatch.setattr(ollama, "summarize", broken_summary)
    monkeypatch.setattr(ollama, "stream_chat", source)
    result = await runtime.submit_message(owner, uuid4(), "你好")
    turn_id = UUID(result["turn_id"])
    events = [event async for event in runtime.subscribe(owner, turn_id)]
    assert events[-1]["status"] == "completed"
    assert events[-1]["message"]["content"] == "仍可正常对话"
    assert set(events[-1]["context_info"]["issues"]) == {"summary_failed", "context_trimmed"}
    assert len((await repo.messages(owner, session_id))["items"]) == 10


async def test_orphan_subscription_finishes_and_next_submit_recovers(
    owner, monkeypatch, ollama, repo, runtime, transaction
):
    """无内存任务的 running 应在订阅时收尾，不空转；新提交也能自行恢复遗留轮次。"""
    session_id = UUID((await fixture_session(owner, transaction))["id"])
    orphan, _ = await repo.create_turn(owner, session_id, uuid4(), "遗留问题")
    events = await asyncio.wait_for(collect_events(runtime.subscribe(owner, orphan)), timeout=2)
    assert events[-1]["status"] == "interrupted"
    orphan, _ = await repo.create_turn(owner, session_id, uuid4(), "另一个遗留问题")

    async def source(messages):
        yield {"type": "delta", "text": "可以继续"}
        yield {"type": "done", "partial": False}

    monkeypatch.setattr(ollama, "stream_chat", source)
    result = await runtime.submit_message(owner, uuid4(), "新问题")
    assert (await repo.turn_snapshot(owner, orphan))["status"] == "interrupted"
    events = await collect_events(runtime.subscribe(owner, UUID(result["turn_id"])))
    assert events[-1]["status"] == "completed"


async def collect_events(stream):
    return [event async for event in stream]


@pytest.mark.parametrize("model_fails", [False, True])
async def test_failed_terminal_write_recovers_without_restart(
    owner, monkeypatch, model_fails, ollama, repo, runtime, transaction
):
    """故障注入：模型成功或失败时终态写库失败，恢复后补写原状态并解除会话忙碌。"""
    session_id = UUID((await fixture_session(owner, transaction))["id"])
    original = repo.checkpoint
    unavailable = True

    async def broken_checkpoint(*args, **kwargs):
        if unavailable:
            raise ConnectionError("模拟数据库不可写")
        return await original(*args, **kwargs)

    async def source(messages):
        yield {"type": "delta", "text": "已生成正文"}
        if model_fails:
            raise RuntimeError("模拟模型断流")
        yield {"type": "done", "partial": False, "usage": {"completion_tokens": 5}}

    monkeypatch.setattr(repo, "checkpoint", broken_checkpoint)
    monkeypatch.setattr(ollama, "stream_chat", source)
    result = await runtime.submit_message(owner, uuid4(), "你好")
    turn_id = UUID(result["turn_id"])
    task = runtime.running[turn_id].task
    await asyncio.wait_for(task, 2)
    assert turn_id in runtime.pending_terminals
    assert (await repo.turn_snapshot(owner, turn_id))["status"] == "running"
    await runtime.flush_pending()  # 持续不可写时保留补写记录，不冒充保存成功。
    assert turn_id in runtime.pending_terminals
    unavailable = False
    await runtime.flush_pending()
    saved = await repo.turn_snapshot(owner, turn_id)
    assert saved["status"] == ("failed" if model_fails else "completed")
    assert saved["message"]["content"] == "已生成正文"
    assert turn_id not in runtime.pending_terminals
    assert (await repo.messages(owner, session_id))["active_turn_id"] is None
    assert (await runtime.submit_message(owner, uuid4(), "继续"))["created"]


async def test_single_session_concurrent_open_and_user_isolation(owner, client, repo, transaction):
    """首次并发打开只能得到一个会话；另一个用户有自己的空历史，旧多会话 API 已移除。"""
    responses = await asyncio.gather(
        *[client.put("/api/v1/chat/session", headers=headers(owner)) for _ in range(5)]
    )
    assert sorted(response.status_code for response in responses) == [200, 200, 200, 200, 201]
    ids = {response.json()["data"]["id"] for response in responses}
    assert len(ids) == 1
    session_id = UUID(ids.pop())
    await fixture_turn(owner, session_id, repo=repo)
    reopened = await client.put("/api/v1/chat/session", headers=headers(owner))
    assert reopened.json()["data"]["id"] == str(session_id)
    other = "0x" + uuid4().hex + "12345678"
    try:
        page = await client.get("/api/v1/chat/session/messages", headers=headers(other))
        assert page.status_code == 200 and page.json()["data"]["items"] == []
        assert (await client.post("/api/v1/chat/sessions", headers=headers(owner))).status_code == 404
        async with transaction(owner) as db:
            assert await db.scalar(select(func.count()).select_from(ChatSession)) == 1
    finally:
        await repo.delete_all(other)


async def test_legacy_sessions_are_retained_and_selection_is_stable(owner, repo, runtime, transaction):
    """单会话升级只固定继续使用最近创建的非空会话，不删旧数据，不因回答时间而切换。"""
    first = UUID((await fixture_session(owner, transaction))["id"])
    await fixture_turn(owner, first, repo=repo)
    second = UUID((await fixture_session(owner, transaction))["id"])
    await fixture_turn(owner, second, repo=repo)
    await fixture_session(owner, transaction)  # 较新的空会话不能遮住已有历史。
    assert (await runtime.session(owner))["id"] == str(second)
    await fixture_turn(owner, first, repo=repo)
    assert (await runtime.session(owner))["id"] == str(second)
    assert len((await repo.messages(owner, first))["items"]) == 4


async def test_cancel_waits_for_submit_registration(owner, monkeypatch, ollama, repo, runtime, transaction):
    """在提交入库与任务登记之间取消，也必须找到迟登记任务并关闭模型生成。"""
    await fixture_session(owner, transaction)
    real_snapshot = repo.turn_snapshot
    entered, resume, model_gate = asyncio.Event(), asyncio.Event(), asyncio.Event()
    first = True
    calls = []
    ids = []

    async def paused_snapshot(address, turn_id):
        nonlocal first
        if first:
            first = False
            ids.append(turn_id)
            entered.set()
            await resume.wait()
        return await real_snapshot(address, turn_id)

    async def source(messages):
        await model_gate.wait()
        calls.append(messages)
        yield {"type": "delta", "text": "不应生成"}
        yield {"type": "done", "partial": False}

    monkeypatch.setattr(repo, "turn_snapshot", paused_snapshot)
    monkeypatch.setattr(ollama, "stream_chat", source)
    submission = asyncio.create_task(runtime.submit_message(owner, uuid4(), "你好"))
    await asyncio.wait_for(entered.wait(), 2)
    cancellation = asyncio.create_task(runtime.cancel(owner, ids[0]))
    await asyncio.sleep(0)  # 让取消请求进入等待用户锁的窗口。
    resume.set()
    await asyncio.wait_for(submission, 2)
    result = await asyncio.wait_for(cancellation, 2)
    model_gate.set()
    assert result["status"] == "cancelled"
    assert not runtime.has_live_worker(ids[0])
    assert not calls


async def test_physical_lock_connection_loss_requires_restart(
    owner, monkeypatch, ollama, repo, runtime, services
):
    """仅断开测试运行时自己的锁连接：旧实例停调度且不写终态，新实例启动才能收尾。"""
    await runtime.stop()
    old, replacement = fresh_runtime(services), fresh_runtime(services)
    entered = asyncio.Event()

    async def source(messages):
        yield {"type": "delta", "text": "生成中"}
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ollama, "stream_chat", source)
    try:
        await old.start()
        await old.session(owner)  # 真实 pg_locks 检查必须能辨认本连接的会话锁。
        result = await old.submit_message(owner, uuid4(), "你好")
        turn_id = UUID(result["turn_id"])
        task = old.running[turn_id].task
        await asyncio.wait_for(entered.wait(), 2)
        await old.lock.connection.invalidate()  # 不重启数据库，不触碰其他进程的连接。
        with pytest.raises(ChatUnavailableError):
            await old.verify_lease()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        assert (await repo.turn_snapshot(owner, turn_id))["status"] == "running"
        with pytest.raises(ChatUnavailableError):
            await old.submit_message(owner, uuid4(), "拒绝新任务")
        await replacement.start()
        assert (await repo.turn_snapshot(owner, turn_id))["status"] == "interrupted"
    finally:
        await replacement.stop()
        await old.stop()


async def test_history_is_a_consistent_read_snapshot(owner, monkeypatch, services, repo, transaction):
    """在两次 SELECT 之间提交新问题，历史响应必须仍来自同一个快照。"""
    from contextlib import asynccontextmanager

    session_id = UUID((await fixture_session(owner, transaction))["id"])
    original = services.database.transaction
    read, resume = asyncio.Event(), asyncio.Event()

    class Proxy:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, key):
            return getattr(self.db, key)

        async def scalars(self, statement):
            result = await self.db.scalars(statement)
            read.set()
            await resume.wait()
            return result

    @asynccontextmanager
    async def paused(address, **kwargs):
        async with original(address, **kwargs) as db:
            yield Proxy(db)

    monkeypatch.setattr(services.database, "transaction", paused)
    reading = asyncio.create_task(repo.messages(owner, session_id))
    try:
        await asyncio.wait_for(read.wait(), 2)
        await repo.create_turn(owner, session_id, uuid4(), "并发提交")
        resume.set()
        page = await asyncio.wait_for(reading, 2)
        assert page["items"] == [] and page["active_turn_id"] is None
    finally:
        resume.set()
        await asyncio.gather(reading, return_exceptions=True)


async def test_rest_reads_do_not_create_or_write_terminal(
    owner, client, repo, runtime, monkeypatch, transaction
):
    """GET 空历史不插会话；孤儿快照不在请求栈补写，后台维护独立收尾。"""
    response = await client.get("/api/v1/chat/session/messages", headers=headers(owner))
    assert response.json()["data"]["items"] == []
    assert await repo.find_session(owner) is None
    assert (await client.get("/api/v1/chat/session", headers=headers(owner))).status_code == 404
    opened = await client.put("/api/v1/chat/session", headers=headers(owner))
    assert opened.status_code == 201
    assert (await client.get(opened.headers["Location"], headers=headers(owner))).status_code == 200
    session_id = UUID(opened.json()["data"]["id"])
    orphan, _ = await repo.create_turn(owner, session_id, uuid4(), "孤儿问题")
    runtime.recovery_task.cancel()
    await asyncio.gather(runtime.recovery_task, return_exceptions=True)
    runtime.recovery_task = None
    original = repo.checkpoint

    async def forbidden(*args, **kwargs):
        pytest.fail("GET 不得直接补写业务终态")

    monkeypatch.setattr(repo, "checkpoint", forbidden)
    read = await client.get(f"/api/v1/chat/turns/{orphan}", headers=headers(owner))
    assert read.json()["data"]["status"] == "running"
    assert (await repo.turn_snapshot(owner, orphan))["status"] == "running"
    monkeypatch.setattr(repo, "checkpoint", original)
    await runtime.flush_pending()
    assert (await repo.turn_snapshot(owner, orphan))["status"] == "interrupted"


async def test_rest_turn_location_retry_and_idempotent_cancellation(
    owner, client, repo, runtime, monkeypatch, ollama
):
    """202 跟踪链接可读，PUT 取消幂等，重试通过创建新轮次完成，旧动作 URL 不存在。"""
    entered = asyncio.Event()

    async def source(messages):
        entered.set()
        await asyncio.Event().wait()
        yield {}

    monkeypatch.setattr(ollama, "stream_chat", source)
    await client.put("/api/v1/chat/session", headers=headers(owner))
    submitted = await client.post(
        "/api/v1/chat/session/turns",
        headers=headers(owner),
        json={"message": "你好", "client_msg_id": str(uuid4())},
    )
    assert submitted.status_code == 202
    location = submitted.headers["Location"]
    assert submitted.json()["code"] == 0 and submitted.json()["msg"] == "success"
    await entered.wait()
    snapshot = await client.get(location, headers=headers(owner))
    turn_id = snapshot.json()["data"]["turn_id"]
    cancellation = f"/api/v1/chat/turns/{turn_id}/cancellation"
    for _ in range(2):
        cancelled = await client.put(cancellation, headers=headers(owner))
        assert cancelled.status_code == 200 and cancelled.json()["data"]["status"] == "cancelled"
    entered.clear()
    retried = await client.post(
        "/api/v1/chat/session/turns",
        headers=headers(owner),
        json={"retry_of": turn_id, "client_msg_id": str(uuid4())},
    )
    assert retried.status_code == 202 and retried.json()["data"]["turn_id"] != turn_id
    await entered.wait()
    await client.put(
        f"/api/v1/chat/turns/{retried.json()['data']['turn_id']}/cancellation", headers=headers(owner)
    )
    assert (await client.delete("/api/v1/chat/data", headers=headers(owner))).content == b""
    assert (await client.delete("/api/v1/chat/data", headers=headers(owner))).status_code == 204


async def test_cancel_and_delete_release_unstarted_task_state(owner, repo, runtime, transaction):
    """未进入 generate 的任务不会执行 finally，取消和清空也须回收内存登记。"""
    from app.ai.conversation.service import Running

    session_id = UUID((await fixture_session(owner, transaction))["id"])
    turn_id, _ = await repo.create_turn(owner, session_id, uuid4(), "问题")
    state = Running(owner, session_id, await repo.turn_snapshot(owner, turn_id))
    state.task = asyncio.create_task(asyncio.Event().wait())
    runtime.running[turn_id] = state
    snapshot = await runtime.cancel(owner, turn_id)
    assert snapshot["status"] == "cancelled"
    assert state.task.cancelled() and turn_id not in runtime.running
    runtime.queue_recovery(owner, turn_id)
    await runtime.delete_all(owner)
    assert turn_id not in runtime.recovery_candidates
    assert await repo.find_session(owner) is None
