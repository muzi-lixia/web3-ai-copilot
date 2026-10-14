"""文件/数据库提交边界与可恢复清理。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import ConflictError
from app.foundation.config import FoundationSettings
from app.modules.knowledge.service import KnowledgeService
from app.workers.knowledge.config import WorkerSettings
from app.workers.knowledge.consumer import KnowledgeConsumer


@pytest.fixture
def service(tmp_path):
    value = KnowledgeService(
        SimpleNamespace(), FoundationSettings(_env_file=None, knowledge_storage_dir=str(tmp_path))
    )
    value.repository = SimpleNamespace(
        find_by_hash=AsyncMock(return_value=None),
        schedule_cleanup=AsyncMock(),
        create=AsyncMock(return_value=True),
        read=AsyncMock(return_value={"id": "d", "filename": "a.txt", "status": "ready"}),
        delete=AsyncMock(return_value=True),
        finish_cleanup=AsyncMock(),
    )
    return value


async def test_concurrent_duplicate_is_conflict_with_cleanup_intent(service, tmp_path):
    service.repository.create.return_value = False
    with pytest.raises(ConflictError) as error:
        await service.upload("rules.txt", b"rules")
    assert error.value.code == "document_exists"
    assert service.repository.schedule_cleanup.await_count == 2
    assert service.repository.schedule_cleanup.await_args.kwargs == {}
    assert not list(tmp_path.glob("*.upload"))


async def test_ambiguous_database_commit_keeps_original_and_cleanup_intent(service, tmp_path):
    service.repository.create.side_effect = RuntimeError("connection lost after commit")
    with pytest.raises(RuntimeError):
        await service.upload("rules.txt", b"rules")
    # 不能因提交响应丢失就删原件；清理任务查询不到文档引用时才会删除。
    assert len(list(tmp_path.glob("*.txt"))) == 1
    service.repository.schedule_cleanup.assert_awaited_once()


async def test_write_failure_leaves_durable_cleanup_intent(service, monkeypatch):
    monkeypatch.setattr(service, "_write", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        await service.upload("rules.txt", b"rules")
    service.repository.create.assert_not_awaited()
    service.repository.schedule_cleanup.assert_awaited_once()


async def test_delete_disk_failure_does_not_lose_cleanup_job(service, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(
        Path, "unlink", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk failure"))
    )
    await service.remove("d")
    service.repository.delete.assert_awaited_once_with("d")
    service.repository.finish_cleanup.assert_not_awaited()


async def test_worker_retries_failed_cleanup(tmp_path, monkeypatch):
    from pathlib import Path

    consumer = KnowledgeConsumer(
        SimpleNamespace(), WorkerSettings(_env_file=None, knowledge_storage_dir=str(tmp_path))
    )
    from contextlib import asynccontextmanager

    from app.modules.knowledge.repository import CleanupAttempt

    attempt = CleanupAttempt(".txt")

    @asynccontextmanager
    async def guard(*args):
        yield attempt

    consumer.repository = SimpleNamespace(
        cleanup_candidates=AsyncMock(return_value=[{"document_id": "d", "suffix": ".txt"}]),
        cleanup_guard=guard,
        finish_cleanup=AsyncMock(),
    )
    monkeypatch.setattr(
        Path, "unlink", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk failure"))
    )
    await consumer.cleanup_files()
    assert attempt.failed is True
