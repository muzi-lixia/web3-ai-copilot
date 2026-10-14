"""真实 PG 并发租约回归：独立临时 schema，不修改业务文档或迁移版本。"""

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.infrastructure.database.observability import ObservedSession, instrument_engine
from app.modules.knowledge.repository import KnowledgeRepository

pytestmark = pytest.mark.skipif(os.getenv("RUN_DATABASE_TESTS") != "1", reason="需要显式启用本地 PostgreSQL")


@pytest.fixture
async def store():
    database_url = os.getenv(
        "KNOWLEDGE_TEST_DATABASE_URL",
        "postgresql+asyncpg://copilot_admin:copilot_dev_admin@127.0.0.1:54329/web3copilot",
    )
    schema = "lease_test_" + uuid4().hex
    role = "lease_role_" + uuid4().hex
    admin = create_async_engine(database_url)
    engine = None
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
            await connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
        engine = create_async_engine(
            database_url,
            connect_args={"server_settings": {"search_path": f"{schema},public"}},
        )
        instrument_engine(engine)
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE agent_conversations(id text PRIMARY KEY)"))
            await connection.execute(
                text("""CREATE TABLE documents (
                id text PRIMARY KEY, title text NOT NULL DEFAULT 'test', filename text NOT NULL,
                status text NOT NULL DEFAULT 'pending', mime text DEFAULT 'text/plain',
                size_bytes bigint DEFAULT 1, sha256 text UNIQUE,
                chunk_count integer NOT NULL DEFAULT 0, error text, stage text, error_code text,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now())""")
            )
            await connection.execute(
                text("""CREATE TABLE chunks (
                document_id text REFERENCES documents(id) ON DELETE CASCADE, ordinal integer,
                heading text, content text, page_start integer, page_end integer, kind text,
                embedding vector, PRIMARY KEY(document_id, ordinal))""")
            )
            await connection.execute(
                text("""CREATE TABLE agent_turns (
                    id text PRIMARY KEY DEFAULT gen_random_uuid()::text, user_id text,
                    state text, answer text DEFAULT '', results jsonb DEFAULT '[]',
                    error_code text, usage jsonb DEFAULT '{}', tool_trace jsonb DEFAULT '[]',
                    version integer DEFAULT 0, created_at timestamptz DEFAULT now())""")
            )
            await connection.execute(text("CREATE TABLE service_users(id text)"))
            # 实际执行新增迁移，验证旧 processing 行被安全重新排队及租约约束。
            await connection.execute(
                text("INSERT INTO documents(id,filename,status) VALUES('d','a.txt','processing')")
            )

            # 版本目录不是 Python 包，按文件加载迁移。
            def upgrade(sync_connection):
                from importlib.util import module_from_spec, spec_from_file_location
                from pathlib import Path

                for filename in (
                    "0012_knowledge_lease.py",
                    "0013_embedding_version.py",
                    "0014_runtime_operations.py",
                    "0015_observability.py",
                ):
                    path = Path(__file__).parents[2] / "alembic/versions" / filename
                    spec = spec_from_file_location("knowledge_migration", path)
                    module = module_from_spec(spec)
                    spec.loader.exec_module(module)
                    if hasattr(module, "WORKER_ROLE"):
                        module.WORKER_ROLE = role
                    with Operations.context(MigrationContext.configure(sync_connection)):
                        module.upgrade()

            await connection.run_sync(upgrade)
        database = SimpleNamespace(
            sessions=async_sessionmaker(engine, class_=ObservedSession, expire_on_commit=False),
            test_role=role
        )
        yield KnowledgeRepository(database), database
    finally:
        if engine:
            await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await admin.dispose()


async def expire(database):
    async with database.sessions.begin() as session:
        await session.execute(text("UPDATE documents SET lease_until=clock_timestamp()-interval '1 second'"))


def chunk(content):
    return dict(
        ordinal=0,
        heading=None,
        content=content,
        page_start=None,
        page_end=None,
        kind="text",
        embedding="[1,0,0]",
        embedding_version="test-version",
    )


async def test_concurrent_claim_and_recovery_preserve_live_owner(store):
    repository, _ = store
    first, second = await asyncio.gather(repository.claim("a", "t1", 60), repository.claim("b", "t2", 60))
    assert sum(item is not None for item in (first, second)) == 1
    assert await repository.retry_stuck(retry_delay=0) == 0
    assert await repository.claim("c", "t3", 60) is None


async def test_expired_execution_cannot_renew_complete_or_fail_after_takeover(store):
    repository, database = store
    assert await repository.claim("a", "old", 60)
    await expire(database)
    assert not await repository.renew("d", "old", 60)
    assert not await repository.complete("d", "old", [chunk("expired")])
    assert not await repository.fail("d", "old", "expired", stage="load", error_code="old")
    assert await repository.retry_stuck(retry_delay=0) == 1
    assert await repository.claim("b", "new", 60)
    assert not await repository.renew("d", "old", 60)
    assert not await repository.complete("d", "old", [chunk("stale")])
    assert await repository.renew("d", "new", 60)
    assert await repository.complete("d", "new", [chunk("new result")])
    assert not await repository.fail("d", "old", "late failure", stage="load", error_code="old")
    async with database.sessions() as session:
        assert await session.scalar(text("SELECT content FROM chunks")) == "new result"
        row = (
            await session.execute(text("SELECT status,claim_token,worker_id,lease_until FROM documents"))
        ).one()
        assert tuple(row) == ("ready", None, None, None)


async def test_deleted_task_cannot_be_resurrected(store):
    repository, _ = store
    assert await repository.claim("a", "t", 60)
    assert await repository.delete("d")
    assert not await repository.complete("d", "t", [chunk("deleted")])
    assert not await repository.fail("d", "t", "failure", stage="load", error_code="deleted")


async def test_chunk_failure_rolls_back_status_and_preserves_lease(store):
    from sqlalchemy.exc import DBAPIError

    repository, database = store
    assert await repository.claim("a", "t", 60)
    invalid = chunk("invalid")
    invalid["embedding"] = "invalid-vector"
    with pytest.raises(DBAPIError):
        await repository.complete("d", "t", [invalid])
    async with database.sessions() as session:
        row = (await session.execute(text("SELECT status,claim_token FROM documents"))).one()
        assert tuple(row) == ("processing", "t")
        assert await session.scalar(text("SELECT count(*) FROM chunks")) == 0
    assert await repository.complete("d", "t", [chunk("valid")])


async def test_failure_retry_rejects_previous_execution_token(store):
    repository, database = store
    assert await repository.claim("a", "old", 60)
    assert await repository.fail("d", "old", "parse failed", stage="load", error_code="parse")
    assert await repository.retry("d")
    assert await repository.claim("a", "new", 60)
    assert not await repository.complete("d", "old", [chunk("stale")])
    assert not await repository.fail("d", "old", "stale", stage="load", error_code="old")
    assert await repository.complete("d", "new", [chunk("retried")])
    async with database.sessions() as session:
        assert await session.scalar(text("SELECT content FROM chunks")) == "retried"


async def test_search_excludes_legacy_or_different_vector_versions(store):
    repository, database = store
    assert await repository.claim("a", "t", 60)
    assert await repository.complete("d", "t", [chunk("current")])
    assert len(await repository.search("[1,0,0]", 5, "test-version")) == 1
    assert await repository.search("[1,0,0]", 5, "different-model-same-dimensions") == []
    assert await repository.index_status("different-model-same-dimensions") == {"ready": 1, "compatible": 0}
    async with database.sessions.begin() as session:
        await session.execute(text("UPDATE chunks SET embedding_version=NULL"))
    assert await repository.search("[1,0,0]", 5, "test-version") == []
    async with database.sessions.begin() as session:
        await session.execute(text("UPDATE chunks SET embedding_version='test-version'"))
        await session.execute(text("UPDATE documents SET embedding_version=NULL"))
    assert await repository.search("[1,0,0]", 5, "test-version") == []


async def test_reindex_removes_old_chunks_and_requires_new_execution(store):
    repository, database = store
    assert await repository.claim("a", "old", 60)
    assert not await repository.reindex("d")
    assert await repository.complete("d", "old", [chunk("old")])
    assert await repository.reindex("d")
    async with database.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM chunks")) == 0
        assert await session.scalar(text("SELECT embedding_version FROM documents")) is None
    assert not await repository.complete("d", "old", [chunk("stale")])
    assert await repository.claim("a", "new", 60)
    current = {**chunk("new model"), "embedding_version": "v2"}
    assert await repository.complete("d", "new", [current])
    assert len(await repository.search("[1,0,0]", 5, "v2")) == 1
    assert not await repository.search("[1,0,0]", 5, "test-version")


async def test_cleanup_intents_survive_delete_and_ignore_live_references(store):
    repository, database = store
    await repository.schedule_cleanup("d", ".txt")
    assert await repository.cleanup_candidates() == []
    assert await repository.delete("d")
    jobs = await repository.cleanup_candidates()
    assert jobs == [{"document_id": "d", "suffix": ".txt"}]
    await repository.finish_cleanup("d", failed=True)
    assert await repository.cleanup_candidates() == []
    async with database.sessions() as session:
        assert await session.scalar(text("SELECT attempts FROM knowledge_file_deletions")) == 1
    await repository.finish_cleanup("d")


async def test_duplicate_create_is_atomic_and_success_cancels_cleanup(store):
    repository, database = store
    payload = dict(id="first", title="title", filename="file.txt", mime="text/plain", size=1, sha256="hash")
    await repository.schedule_cleanup("first", ".txt", delay=3600)
    assert await repository.create(payload)
    await repository.schedule_cleanup("second", ".txt")
    assert not await repository.create({**payload, "id": "second"})
    async with database.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM documents WHERE sha256='hash'")) == 1
        assert (
            await session.scalar(
                text("SELECT count(*) FROM knowledge_file_deletions WHERE document_id='first'")
            )
            == 0
        )


async def test_repeated_crash_exhausts_budget_and_manual_retry_resets_it(store):
    repository, database = store
    for attempt in range(3):
        assert await repository.claim("worker", f"token-{attempt}", 60)
        await expire(database)
        assert await repository.retry_stuck(max_attempts=3, retry_delay=0) == 1
    assert await repository.claim("worker", "fourth", 60) is None
    async with database.sessions() as session:
        row = (await session.execute(text("SELECT status,error_code FROM documents WHERE id='d'"))).one()
        assert tuple(row) == ("failed", "task_attempts_exhausted")
    assert await repository.retry("d")
    assert await repository.claim("worker", "manual", 60)


async def test_worker_role_can_process_but_cannot_access_identity_or_agent_rows(store):
    from sqlalchemy.exc import DBAPIError

    repository, database = store
    # 使用同一个连接并切换到迁移实际授权的临时角色，验证整条仓储写入链。
    async with database.sessions.kw["bind"].connect() as connection:
        await connection.execute(text(f'SET ROLE "{database.test_role}"'))
        await connection.commit()
        scoped = KnowledgeRepository(SimpleNamespace(sessions=async_sessionmaker(connection)))
        try:
            assert await scoped.claim("worker", "t", 60)
            assert await scoped.complete("d", "t", [chunk("allowed")])
            await scoped.heartbeat("worker")
            assert (await scoped.queue_status())["workers"] == 1
            for query in (
                "SELECT * FROM service_users",
                "SELECT * FROM agent_turns",
                "UPDATE documents SET filename='tampered.txt' WHERE id='d'",
                "DELETE FROM documents WHERE id='d'",
            ):
                with pytest.raises(DBAPIError):
                    await connection.execute(text(query))
                await connection.rollback()
        finally:
            await connection.execute(text("RESET ROLE"))
            await connection.commit()


async def test_agent_aggregate_status_returns_only_summary_metrics(store):
    _, database = store
    async with database.sessions.begin() as session:
        await session.execute(
            text("INSERT INTO agent_turns(state,timings) VALUES('completed',CAST(:timings AS jsonb))"),
            {"timings": '{"queue_ms":10,"first_token_ms":20,"total_ms":30}'},
        )
        row = (await session.execute(text("SELECT * FROM agent_runtime_status()"))).mappings().one()
        assert dict(row) == {
            "state": "completed",
            "count": 1,
            "queue_p95_ms": 10,
            "first_token_p95_ms": 20,
            "total_p95_ms": 30,
        }


async def test_cleanup_guard_prevents_late_document_registration(store):
    from app.core.exceptions import ConflictError

    repository, database = store
    await repository.schedule_cleanup("late", ".txt")
    payload = dict(id="late", title="title", filename="file.txt", mime="text/plain", size=1, sha256="late")
    async with repository.cleanup_guard("late") as attempt:
        assert attempt is not None
        pending = asyncio.create_task(repository.create(payload))
        await asyncio.sleep(0)
    with pytest.raises(ConflictError) as error:
        await pending
    assert error.value.code == "document_upload_expired"
    async with database.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM documents WHERE id='late'")) == 0


async def test_conversation_deletion_durably_queues_checkpoint_cleanup(store):
    _, database = store
    async with database.sessions.begin() as session:
        await session.execute(text("INSERT INTO agent_conversations(id) VALUES('old-thread')"))
    # 删除与清理意图在同一事务；回滚不会产生误删任务。
    with pytest.raises(RuntimeError):
        async with database.sessions.begin() as session:
            await session.execute(text("DELETE FROM agent_conversations WHERE id='old-thread'"))
            raise RuntimeError("rollback")
    async with database.sessions.begin() as session:
        assert await session.scalar(text("SELECT count(*) FROM agent_checkpoint_deletions")) == 0
        await session.execute(text("DELETE FROM agent_conversations WHERE id='old-thread'"))
    async with database.sessions() as session:
        assert await session.scalar(text("SELECT thread_id FROM agent_checkpoint_deletions")) == "old-thread"
        row = (await session.execute(text("SELECT * FROM agent_observability_status()"))).mappings().one()
        assert row["checkpoint_pending"] == 1


async def test_upload_trace_survives_database_claim_boundary(store):
    from app.core.logging import trace_id

    repository, _ = store
    await repository.schedule_cleanup("traced", ".txt")
    token = trace_id.set("f" * 32)
    try:
        await repository.create(dict(id="traced", title="t", filename="t.txt", mime="text/plain",
                                     size=1, sha256="traced"))
    finally:
        trace_id.reset(token)
    # 旧迁移测试用例中存在一篇重新排队的文档，明确清除以隔离领取顺序。
    await repository.delete("d")
    claimed = await repository.claim("worker", "claim", 60)
    assert claimed["trace_id"] == "f" * 32
    assert claimed["attempt_count"] == 1


async def test_replayed_or_late_terminal_update_cannot_overwrite_finished_turn(store):
    from app.agent.config import AgentSettings
    from app.agent.repository import ConversationRepository
    from app.infrastructure.database.session import Database

    _, database = store
    adapter = Database.__new__(Database)
    adapter.sessions = database.sessions
    repository = ConversationRepository(adapter, AgentSettings(_env_file=None))
    async with database.sessions.begin() as session:
        await session.execute(text("INSERT INTO agent_turns(id,user_id,state) VALUES('turn','u','queued')"))
    await repository.update("u", "turn", "completed answer", "completed", [])
    await repository.update("u", "turn", "late stale error", "failed", [], error="service_restarted")
    async with database.sessions() as session:
        row = (await session.execute(text("SELECT answer,state,version FROM agent_turns"))).one()
        assert tuple(row) == ("completed answer", "completed", 1)
