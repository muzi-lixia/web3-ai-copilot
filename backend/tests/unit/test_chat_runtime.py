"""无需数据库的生命周期与摘要预算回归；真实并发和 RLS 另由 PostgreSQL 测试验证。"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.ai.conversation.service import Running
from app.ai.memory import service as context
from app.core.config import settings
from app.core.container import ApplicationServices
from app.core.exceptions import ChatUnavailableError


@pytest.fixture
async def services():
    services = ApplicationServices.build(settings)
    yield services
    await services.close()


class Lease:
    """模拟持锁连接，记录显式解锁及关闭；支持恢复失败和锁丢失。"""

    def __init__(self, held=True, fail_recovery=False):
        self.held = held
        self.fail_recovery = fail_recovery
        self.invalidated = False
        self.closed = False
        self.unlocked = False

    async def scalar(self, statement):
        return self.held

    async def execute(self, statement):
        if "recover_interrupted" in str(statement) and self.fail_recovery:
            raise RuntimeError("模拟启动恢复失败")
        if "advisory_unlock" in str(statement):
            self.unlocked = True

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def invalidate(self):
        self.invalidated = True

    async def close(self):
        self.closed = True


async def test_start_failure_releases_session_lock(monkeypatch, services):
    lease = Lease(fail_recovery=True)

    async def connect():
        return lease

    runtime = services.chat
    runtime.lock.engine = SimpleNamespace(connect=connect)

    async def failed_recovery():
        raise RuntimeError("模拟启动恢复失败")

    monkeypatch.setattr(services.repository, "recover_interrupted", failed_recovery)
    with pytest.raises(RuntimeError, match="启动恢复失败"):
        await runtime.start()
    assert runtime.lock.connection is None and lease.closed and lease.unlocked
    assert runtime.stopping


@pytest.mark.parametrize("invalidated", [False, True])
async def test_lost_lease_stops_tasks_and_never_writes_terminal(monkeypatch, invalidated, services):
    runtime = services.chat
    runtime.lock.connection = Lease(held=False)
    runtime.lock.connection.invalidated = invalidated
    turn_id = uuid4()
    state = Running("owner", uuid4(), {"version": 1, "message": {"content": "半段正文"}})
    state.task = asyncio.create_task(asyncio.Event().wait())
    runtime.running[turn_id] = state

    async def forbidden(*args, **kwargs):
        pytest.fail("丢失运行锁后不允许旧实例写入状态")

    monkeypatch.setattr(services.repository, "checkpoint", forbidden)
    with pytest.raises(ChatUnavailableError):
        await runtime.verify_lease()
    await asyncio.gather(state.task, return_exceptions=True)
    assert state.task.cancelled() and runtime.stopping
    assert not await runtime.finalize(turn_id, state, "半段正文", "cancelled")
    assert state.snapshot["status"] == "interrupted"
    assert not runtime.pending_terminals
    with pytest.raises(ChatUnavailableError):
        await runtime.submit_message("owner", uuid4(), "不应接受")


async def test_summary_batches_include_previous_memory_and_never_drop_fragments(monkeypatch, services):
    repo, ollama, memory_service = services.repository, services.model, services.memory
    """合法的大旧摘要与长英文原文必须动态拆批，每个完整请求安全，原文片段全部被处理。"""
    ids = [str(uuid4()) for _ in range(20)]
    memory = {key: [] for key in context.KEYS}
    memory["facts"] = [{"text": "a" * 180, "sources": [id]} for id in ids]
    context.validate_summary(memory, set(ids))
    previous = SimpleNamespace(version=1, covered_through_seq=2, content=memory, source_message_ids=ids)
    pairs = []
    for index in range(10):
        pairs.append(
            {
                "seq": index * 2 + 2,
                "messages": [
                    {"id": str(uuid4()), "role": "user", "content": "a" * 2000},
                    {"id": str(uuid4()), "role": "assistant", "content": "b" * 2000},
                ],
            }
        )

    async def data(*args):
        return pairs, previous

    calls = []

    async def summarize(messages):
        ollama.validate_context(messages, summary=True)
        assert context.cost(messages) <= settings.copilot_input_budget
        calls.append(json.loads(messages[-1]["content"])["messages"])
        return memory

    saved = []

    async def save(*args):
        saved.append(args)

    monkeypatch.setattr(repo, "context_data", data)
    monkeypatch.setattr(repo, "save_summary", save)
    monkeypatch.setattr(ollama, "summarize", summarize)
    await memory_service.compact("owner", uuid4(), force=True)
    selected = pairs[1 : -settings.copilot_recent_turns]
    expected = {message["id"]: message["content"] for pair in selected for message in pair["messages"]}
    actual = {}
    for batch in calls:
        for fragment in batch:
            actual[fragment["id"]] = actual.get(fragment["id"], "") + fragment["content"]
    assert actual == expected and len(calls) > 1
    assert len(saved) == 1 and saved[0][3] == selected[-1]["seq"]


async def test_safe_budget_attempts_summary_before_trimming(monkeypatch, services):
    """长中文超出安全预算时先摘要，不能只依据粗估阈值跳过摘要。"""
    for name, value in {
        "copilot_summary_trigger": 7000,
        "copilot_input_budget": 10000,
        "copilot_context_window": 16384,
        "copilot_output_tokens": 1024,
        "copilot_context_reserve": 1024,
    }.items():
        monkeypatch.setattr(settings, name, value)
    pairs = [
        {
            "seq": i * 2 + 2,
            "messages": [
                {"id": str(uuid4()), "role": "user", "content": "中" * 300},
                {"id": str(uuid4()), "role": "assistant", "content": "文" * 300},
            ],
        }
        for i in range(8)
    ]

    async def data(*args):
        return pairs, None

    called = []

    async def compact(*args, **kwargs):
        called.append(True)
        return SimpleNamespace(version=1, covered_through_seq=4, content={"facts": []})

    monkeypatch.setattr(services.repository, "context_data", data)
    monkeypatch.setattr(services.memory, "compact", compact)
    messages, version, info = await services.memory.build_context("owner", uuid4(), "你好")
    assert called == [True] and version == 1
    assert "context_trimmed" not in info["issues"]
    assert services.model.fits_context(messages)
