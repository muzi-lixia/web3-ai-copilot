"""追踪隐私、业务状态和跨请求关联的回归。"""

import json
import logging
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from langsmith import Client

from app.agent.config import AgentSettings
from app.agent.outbox import TerminalOutbox
from app.agent.privacy import SafeInput
from app.agent.service import ChatService
from app.agent.tracing import anonymize, content_anonymizer, safe_metadata
from app.core.exceptions import UnauthorizedError
from app.core.logging import JsonFormatter, log_context, span, trace_id
from scripts.check_runtime import alerts


def test_exception_logs_have_code_location_without_sensitive_content():
    try:
        raise ValueError("Bearer secret-token https://private.service/path asset=12345")
    except ValueError:
        record = logging.LogRecord("test", logging.ERROR, __file__, 1,
                                   "test.failed", (), sys.exc_info())
        rendered = JsonFormatter().format(record)
    value = json.loads(rendered)
    assert value["exception_type"] == "ValueError"
    assert value["stack"][-1]["file"] == "test_observability.py"
    for secret in ("secret-token", "private.service", "12345", "/Users/"):
        assert secret not in rendered


def test_langsmith_sdk_redacts_error_inputs_outputs_and_metadata():
    # 使用安装版本的真实序列化过滤器，阻止 SDK 选项变化导致隐私回归；不调用网络。
    client = Client(api_url="http://localhost:1", api_key="test", auto_batch_tracing=False,
                    hide_inputs=True, hide_outputs=True, hide_metadata=safe_metadata,
                    anonymizer=anonymize)
    try:
        trace = "a" * 32
        assert client._hide_run_error("Bearer secret https://private asset=123") == "redacted"
        assert client._hide_run_inputs({"query": "private text"}) == {}
        assert client._hide_run_outputs({"answer": "asset=123"}) == {}
        assert client._hide_run_metadata({
            "trace_id": trace, "turn_id": str(uuid4()), "credential": "secret",
            "session_id": "Bearer secret", "user_id": "private-user",
        }).keys() == {"trace_id", "turn_id"}
    finally:
        client.close()


def test_handled_failure_is_not_logged_as_success(caplog):
    with caplog.at_level(logging.INFO), log_context(turn_id="turn"):
        with span("turn.completed") as outcome:
            outcome.update(status="failed", error_code="execution_timeout")
    record = next(r for r in caplog.records if r.msg == "turn.completed")
    assert record.status == "failed"
    assert record.error_code == "execution_timeout"


async def test_terminal_outbox_survives_restart_and_is_private(tmp_path):
    directory = tmp_path / "outbox"
    first = TerminalOutbox(str(directory))
    values = {"state": "completed", "answer": "result", "results": []}
    await first.save("turn", "user", values, "a" * 32)
    assert directory.stat().st_mode & 0o777 == 0o700
    path = next(directory.glob("*.json"))
    assert path.stat().st_mode & 0o777 == 0o600
    second = TerminalOutbox(str(directory))
    records = await second.load()
    assert records[0]["values"] == values
    assert records[0]["trace_id"] == "a" * 32
    await second.remove("turn")
    assert await second.load() == []


async def test_bad_outbox_record_fails_without_silently_discarding_it(tmp_path):
    path = tmp_path / "corrupted.json"
    path.write_text("{broken")
    with pytest.raises(ValueError):
        await TerminalOutbox(str(tmp_path)).load()
    assert path.exists()


async def test_turn_failure_status_and_recovery_keep_original_trace(tmp_path, caplog):
    settings = AgentSettings(_env_file=None, agent_outbox_dir=str(tmp_path))
    repository = SimpleNamespace(model_provider=AsyncMock(return_value="ollama"), update=AsyncMock())
    repository.update.side_effect = OSError("database offline")
    client = SimpleNamespace(identity=AsyncMock(side_effect=UnauthorizedError("revoked")))
    lease = SimpleNamespace(verify=AsyncMock())
    chat = ChatService(repository, client, settings, lease, None)
    original = trace_id.set("b" * 32)
    try:
        with caplog.at_level(logging.INFO):
            await chat.generate("u", "c", "t", "permit", SafeInput(text="hello"))
    finally:
        trace_id.reset(original)
    record = next(r for r in caplog.records if r.msg == "turn.completed")
    assert record.status == "failed"
    assert record.error_code == "unauthorized"
    assert (await chat.recovery.outbox.load())[0]["trace_id"] == "b" * 32
    # 新进程重启后，从持久化记录恢复，不重新调用模型。
    restarted = ChatService(repository, client, settings, lease, None)
    for value in await restarted.recovery.outbox.load():
        restarted.recovery.pending[value["turn_id"]] = (value["user"], value["values"])
        restarted.recovery.pending_traces[value["turn_id"]] = value["trace_id"]
    repository.update.side_effect = None
    seen = []

    async def update(*args, **kwargs):
        seen.append(trace_id.get())

    repository.update.side_effect = update
    await restarted.recovery.flush_pending(restarted.streams.broadcast)
    assert seen == ["b" * 32]
    assert restarted.recovery.pending == {}
    assert await restarted.recovery.outbox.load() == []
    assert trace_id.get() == ""


async def test_checkpoint_cleanup_retries_without_removing_failed_intent(tmp_path):
    repository = SimpleNamespace(
        checkpoint_deletions=AsyncMock(return_value=["old-thread"]),
        finish_checkpoint_deletion=AsyncMock(),
    )
    saver = SimpleNamespace(adelete_thread=AsyncMock(side_effect=OSError("offline")))
    chat = ChatService(repository, None,
                       AgentSettings(_env_file=None, agent_outbox_dir=str(tmp_path)), None, saver)
    await chat.recovery.cleanup_checkpoints()
    repository.finish_checkpoint_deletion.assert_not_awaited()
    saver.adelete_thread.side_effect = None
    await chat.recovery.cleanup_checkpoints()
    repository.finish_checkpoint_deletion.assert_awaited_once_with("old-thread")


@pytest.mark.parametrize("service,status,expected", [
    ("knowledge", dict(workers=0, oldest_pending_seconds=301, failed=1, cleanup_pending=101),
     {"knowledge_worker_offline", "knowledge_queue_stalled", "knowledge_failed_documents",
      "knowledge_cleanup_backlog"}),
    ("agent", dict(total=10, failed=3, oldest_active_seconds=301, checkpoint_pending=101,
                   outbox_pending=1),
     {"agent_turn_stalled", "agent_failure_rate", "checkpoint_cleanup_backlog", "agent_persistence_pending"}),
    ("agent", dict(total=1, failed=1, oldest_active_seconds=0,
                   checkpoint_pending=0, outbox_pending=0), set()),
])
def test_runtime_alerts(service, status, expected):
    assert set(alerts(service, status)) == expected


async def test_expired_conversation_returns_retired_thread_id():
    from datetime import UTC, datetime, timedelta

    from app.agent.repository import ConversationRepository

    row = {"id": "old-thread", "expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    result = SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: row))
    session = SimpleNamespace(
        execute=AsyncMock(return_value=result), scalar=AsyncMock(side_effect=[0, "new"])
    )

    @asynccontextmanager
    async def begin():
        yield session

    database = SimpleNamespace(transaction=lambda user: begin())
    repository = ConversationRepository(database, AgentSettings(_env_file=None))
    value = await repository.conversation("u")
    assert value["expired_thread_id"] == "old-thread"


async def test_unhandled_http_error_keeps_trace_header_and_log(caplog):
    import httpx
    from fastapi import FastAPI

    from app.core.http.errors import register_exception_handlers
    from app.core.logging import TraceMiddleware

    app = FastAPI()
    app.add_middleware(TraceMiddleware, service="test-service")
    register_exception_handlers(app)

    @app.get("/broken")
    async def broken():
        raise RuntimeError("private upstream response")

    # formatter 在记录发生时读取上下文；模拟生产 handler，捕获真实 JSON。
    output = []

    class Handler(logging.Handler):
        def emit(self, record):
            output.append(json.loads(JsonFormatter().format(record)))

    handler = Handler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            response = await client.get("/broken", headers={"X-Trace-Id": "c" * 32})
    finally:
        root.removeHandler(handler)
    assert response.status_code == 500
    assert response.headers["X-Trace-Id"] == "c" * 32
    event = next(item for item in output if item["event"] == "http.internal_error")
    assert event["trace_id"] == "c" * 32
    assert event["service"] == "test-service"
    assert "private upstream response" not in json.dumps(output)


async def test_restart_cleans_uncommitted_temporary_files_only(tmp_path):
    outbox = TerminalOutbox(str(tmp_path))
    await outbox.save("t", "u", {"state": "completed"}, "a" * 32)
    leftover = tmp_path / "partial.tmp"
    leftover.write_text("partial business result")
    await outbox.clear_temporary()
    assert not leftover.exists()
    assert len(await outbox.load()) == 1


def test_langsmith_content_mode_keeps_debug_content_and_scrubs_credentials():
    client = Client(api_url="http://localhost:1", api_key="test", auto_batch_tracing=False,
                    hide_inputs=False, hide_outputs=False, hide_metadata=safe_metadata,
                    anonymizer=content_anonymizer(["configured-private-key"]))
    try:
        raw = {"messages": [{"content": "berachain是什么？ Bearer private-token configured-private-key"}],
               "runtime": {"token": "delegation-token"}, "headers": {"Authorization": "private"},
               "api_key": "secret-value"}
        inputs = client._hide_run_inputs(raw)
        assert "berachain是什么？" in inputs["messages"][0]["content"]
        rendered = json.dumps(inputs)
        for secret in ("private-token", "configured-private-key", "delegation-token", "secret-value"):
            assert secret not in rendered
        assert inputs["headers"]["Authorization"] == "[redacted]"
        result = client._hide_run_outputs({"output": "knowledge_index_incompatible：索引版本不兼容"})
        assert "索引版本不兼容" in result["output"]
        error = client._hide_run_error(
            "knowledge_index_incompatible Bearer credential configured-private-key"
        )
        assert "knowledge_index_incompatible" in error and "configured-private-key" not in error
        assert client._hide_run_metadata({"trace_id": "a" * 32, "user_id": "private-user"}) == {
            "trace_id": "a" * 32
        }
    finally:
        client.close()
