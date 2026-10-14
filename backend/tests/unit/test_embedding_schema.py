"""向量配置与实际数据库列不一致时必须在启动处理任务前失败。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.infrastructure.database.session import Database
from app.workers.knowledge import main as worker
from app.workers.knowledge.config import WorkerSettings


@pytest.mark.parametrize("column_dimensions", [1024, 768, -1, None])
async def test_embedding_schema_checks_actual_column(column_dimensions):
    session = SimpleNamespace(scalar=AsyncMock(return_value=column_dimensions))

    @asynccontextmanager
    async def sessions():
        yield session

    database = Database.__new__(Database)
    database.sessions = sessions
    if column_dimensions == 1024:
        await database.validate_embedding_dimensions(1024)
    else:
        with pytest.raises(RuntimeError, match="请先完成向量列迁移"):
            await database.validate_embedding_dimensions(1024)


async def test_worker_schema_failure_closes_database_without_processing(monkeypatch):
    database = SimpleNamespace(
        validate_embedding_dimensions=AsyncMock(side_effect=RuntimeError("schema mismatch")),
        close=AsyncMock(),
    )
    monkeypatch.setattr(worker, "Database", lambda url: database)
    constructor = AsyncMock()
    monkeypatch.setattr(worker, "KnowledgeConsumer", constructor)
    with pytest.raises(RuntimeError, match="schema mismatch"):
        await worker.run_worker(WorkerSettings(_env_file=None), None)
    database.close.assert_awaited_once()
    constructor.assert_not_called()


@pytest.mark.parametrize("ready", [True, False])
async def test_agent_preflight_reports_actionable_migration_error(ready):
    session = SimpleNamespace(scalar=AsyncMock(return_value=ready))

    @asynccontextmanager
    async def sessions():
        yield session

    database = Database.__new__(Database)
    database.sessions = sessions
    if ready:
        await database.validate_agent_schema()
    else:
        with pytest.raises(RuntimeError, match="uv run alembic upgrade head"):
            await database.validate_agent_schema()
