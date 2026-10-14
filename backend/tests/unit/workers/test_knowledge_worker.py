"""HTTP 生命周期不运行文档消费者，独立 worker 的停止顺序保证任务先于连接关闭。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.foundation.config import FoundationSettings
from app.foundation.main import create_app
from app.workers.knowledge import main as worker
from app.workers.knowledge.config import WorkerSettings


async def test_http_serves_without_starting_document_processing(monkeypatch):
    services = SimpleNamespace(
        settings=FoundationSettings(_env_file=None),
        database=SimpleNamespace(close=AsyncMock(), validate_embedding_dimensions=AsyncMock()),
        auth=SimpleNamespace(cleanup=AsyncMock()),
        knowledge=SimpleNamespace(recover=AsyncMock(), run_pending=AsyncMock()),
    )
    monkeypatch.setattr("app.foundation.main.setup_logging", lambda: None)
    app = create_app(services)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/health")).status_code == 200
        services.knowledge.recover.assert_not_awaited()
        services.knowledge.run_pending.assert_not_awaited()
    services.database.close.assert_awaited_once()


async def test_worker_cancels_processing_before_closing_database(monkeypatch):
    started, stopped, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def run_pending():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def close():
        assert stopped.is_set()

    database = SimpleNamespace(
        close=AsyncMock(side_effect=close), validate_embedding_dimensions=AsyncMock()
    )
    consumer = SimpleNamespace(
        run_pending=run_pending,
        worker_id="test",
        repository=SimpleNamespace(heartbeat=AsyncMock(), queue_status=AsyncMock(return_value={})),
    )
    urls = []
    monkeypatch.setattr(worker, "Database", lambda url: (urls.append(url), database)[1])
    monkeypatch.setattr(worker, "KnowledgeConsumer", lambda db, settings: consumer)
    settings = WorkerSettings(_env_file=None, knowledge_database_url="postgresql+asyncpg://worker-test")
    task = asyncio.create_task(worker.run_worker(settings, stop))
    await asyncio.wait_for(started.wait(), 1)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert urls == [settings.knowledge_database_url]
    database.close.assert_awaited_once()


async def test_worker_recovers_after_database_failure(monkeypatch):
    recovered = asyncio.Event()
    attempts = 0

    async def run_pending():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database down")
        recovered.set()
        return False

    task = asyncio.create_task(worker.consume(SimpleNamespace(run_pending=run_pending), 0.001))
    try:
        await asyncio.wait_for(recovered.wait(), 1)
        assert attempts >= 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_worker_config_is_independent_and_shared_embedding_defaults_match():
    from app.modules.knowledge.version import embedding_version

    settings = WorkerSettings(
        _env_file=None, environment="production", knowledge_database_url="postgresql+asyncpg://worker-test"
    )
    assert not hasattr(settings, "jwt_secret")
    assert not hasattr(settings, "agent_database_url")
    assert not hasattr(settings, "model_api_key")
    assert embedding_version(settings) == embedding_version(FoundationSettings(_env_file=None))


def test_worker_can_load_its_own_env_file(tmp_path):
    path = tmp_path / "worker.env"
    path.write_text("KNOWLEDGE_TASK_LEASE_SECONDS=120\nEMBEDDING_REVISION=worker-revision\n")
    settings = WorkerSettings(_env_file=path)
    assert settings.knowledge_task_lease_seconds == 120
    assert settings.embedding_revision == "worker-revision"


async def test_missing_worker_role_reports_initialization_command_and_closes_pool(monkeypatch):
    from asyncpg.exceptions import InvalidPasswordError

    database = SimpleNamespace(
        validate_embedding_dimensions=AsyncMock(side_effect=InvalidPasswordError("authentication failed")),
        close=AsyncMock(),
    )
    monkeypatch.setattr(worker, "Database", lambda url: database)
    consumer = AsyncMock()
    monkeypatch.setattr(worker, "KnowledgeConsumer", consumer)
    with pytest.raises(RuntimeError, match="init_chat_db.py"):
        await worker.run_worker(WorkerSettings(_env_file=None), asyncio.Event())
    consumer.assert_not_called()
    database.close.assert_awaited_once()
