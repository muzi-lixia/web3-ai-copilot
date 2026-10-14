"""租约失败、取消和处理结束时的任务生命周期。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.workers.knowledge.config import WorkerSettings
from app.workers.knowledge.consumer import KnowledgeConsumer


@pytest.fixture
def service():
    value = KnowledgeConsumer(SimpleNamespace(), WorkerSettings(_env_file=None))
    value.repository = SimpleNamespace(
        retry_stuck=AsyncMock(return_value=0),
        claim=AsyncMock(return_value={"id": "d", "filename": "a.txt"}),
        renew=AsyncMock(),
        complete=AsyncMock(),
        fail=AsyncMock(),
        cleanup_candidates=AsyncMock(return_value=[]),
        finish_cleanup=AsyncMock(),
    )
    return value


@pytest.mark.parametrize("outcome", ["lost", "database_error", "cancelled"])
async def test_heartbeat_failure_or_shutdown_cancels_processing(service, outcome):
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def process(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def heartbeat(*args):
        await started.wait()
        await release.wait()
        if outcome == "database_error":
            raise RuntimeError("database unavailable")

    service._process = process
    service._heartbeat = heartbeat
    task = asyncio.create_task(service.run_pending())
    await started.wait()
    if outcome == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        if outcome == "database_error":
            with pytest.raises(RuntimeError):
                await task
        else:
            assert await task is True
    assert cancelled.is_set()
    service.repository.complete.assert_not_awaited()
    service.repository.fail.assert_not_awaited()


async def test_success_cancels_heartbeat_and_uses_unique_claim_tokens(service):
    tokens = []
    heartbeat_stopped = asyncio.Event()

    async def process(document, token):
        tokens.append(token)
        await asyncio.sleep(0)

    async def heartbeat(*args):
        try:
            await asyncio.Event().wait()
        finally:
            heartbeat_stopped.set()

    service._process, service._heartbeat = process, heartbeat
    assert await service.run_pending() is True
    assert heartbeat_stopped.is_set()
    assert await service.run_pending() is True
    assert len(set(tokens)) == 2
    assert service.repository.claim.await_args.args[0] == service.worker_id
    assert service.repository.retry_stuck.await_count == 2


async def test_empty_queue_still_recovers_expired_tasks(service):
    service.repository.claim.return_value = None
    service._process = AsyncMock()
    assert await service.run_pending() is False
    service.repository.retry_stuck.assert_awaited_once()
    service._process.assert_not_awaited()


async def test_missing_original_records_failure_with_claim_token(service, tmp_path):
    service.settings.knowledge_storage_dir = str(tmp_path)
    await service._process({"id": "d", "filename": "missing.txt"}, "token")
    args = service.repository.fail.await_args
    assert args.args[:2] == ("d", "token")
    assert args.kwargs["error_code"] == "load.read_failed"


async def test_heartbeat_renews_until_ownership_is_lost(service, monkeypatch):
    service.repository.renew.side_effect = [True, False]
    sleep = AsyncMock()
    monkeypatch.setattr("app.workers.knowledge.consumer.asyncio.sleep", sleep)
    await service._heartbeat("d", "t")
    assert service.repository.renew.await_count == 2
    service.repository.renew.assert_awaited_with("d", "t", 60)
    sleep.assert_awaited_with(20)


async def test_worker_inherits_upload_trace_and_records_attempt(service, caplog):
    import logging

    from app.core.logging import log_context, operation_fields, trace_id

    service.repository.claim.return_value.update(trace_id="a" * 32, attempt_count=2)
    seen = []

    async def process(*args):
        seen.append((trace_id.get(), operation_fields.get()))
        return "success"

    service._process = process
    with caplog.at_level(logging.INFO), log_context():
        await service.run_pending()
    assert seen[0][0] == "a" * 32
    assert seen[0][1]["attempt_count"] == 2
    assert seen[0][1]["document_id"] == "d"
    assert len(seen[0][1]["attempt_id"]) == 32
    assert trace_id.get() == ""
    task = next(r for r in caplog.records if r.msg == "knowledge.task")
    assert task.status == "success"
