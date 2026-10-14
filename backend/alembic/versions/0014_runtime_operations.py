"""文件清理队列、worker 心跳、任务重试预算与 Agent 时延记录。"""

from sqlalchemy import text

from alembic import op

revision = "0014_runtime_operations"
down_revision = "0013_embedding_version"
branch_labels = depends_on = None
WORKER_ROLE = "app_knowledge"


def upgrade():
    op.execute("ALTER TABLE documents ADD COLUMN attempt_count integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE documents ADD COLUMN available_at timestamptz NOT NULL DEFAULT now()")
    op.execute("ALTER TABLE agent_turns ADD COLUMN timings jsonb NOT NULL DEFAULT '{}'::jsonb")
    # 聚合函数通过迁移角色的维护策略读取，不向运行账号开放跨用户原始轮次。
    connection = op.get_bind()
    schema = "public" if op.get_context().as_sql else connection.scalar(text("SELECT current_schema()"))
    schema = connection.dialect.identifier_preparer.quote_schema(schema)
    op.execute(f"""CREATE FUNCTION agent_runtime_status() RETURNS TABLE(
        state text, count bigint, queue_p95_ms float8, first_token_p95_ms float8, total_p95_ms float8)
        LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        SELECT state,count(*),
            percentile_cont(0.95) WITHIN GROUP(ORDER BY (timings->>'queue_ms')::float8),
            percentile_cont(0.95) WITHIN GROUP(ORDER BY (timings->>'first_token_ms')::float8),
            percentile_cont(0.95) WITHIN GROUP(ORDER BY (timings->>'total_ms')::float8)
        FROM {schema}.agent_turns WHERE created_at>now()-interval '24 hours' GROUP BY state $$""")
    op.execute("REVOKE ALL ON FUNCTION agent_runtime_status() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION agent_runtime_status() TO app_agent")
    op.execute("""CREATE TABLE knowledge_file_deletions (
        document_id text PRIMARY KEY, suffix text NOT NULL,
        attempts integer NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now(),
        created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("""CREATE TABLE knowledge_workers (
        worker_id text PRIMARY KEY, heartbeat_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("CREATE INDEX knowledge_cleanup_available ON knowledge_file_deletions(available_at)")
    op.execute("CREATE INDEX documents_pending_available ON documents(available_at) WHERE status='pending'")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge_file_deletions TO app_foundation")
    op.execute("GRANT SELECT ON knowledge_workers TO app_foundation")
    # 角色由管理员初始化脚本创建，迁移账号不需要 CREATEROLE。
    role = connection.dialect.identifier_preparer.quote(WORKER_ROLE)
    role_name = WORKER_ROLE.replace("'", "''")
    op.execute(f"""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role_name}') THEN
        GRANT SELECT ON documents TO {role};
        GRANT UPDATE(status,chunk_count,error,stage,error_code,updated_at,worker_id,claim_token,
                     lease_until,embedding_version,attempt_count,available_at) ON documents TO {role};
        GRANT SELECT,INSERT,DELETE ON chunks TO {role};
        GRANT SELECT,INSERT,UPDATE,DELETE ON knowledge_file_deletions,knowledge_workers TO {role};
    END IF; END $$""")


def downgrade():
    op.execute("DROP TABLE knowledge_workers")
    op.execute("DROP TABLE knowledge_file_deletions")
    op.execute("DROP INDEX documents_pending_available")
    op.execute("DROP FUNCTION agent_runtime_status()")
    op.execute("ALTER TABLE agent_turns DROP COLUMN timings")
    op.execute("ALTER TABLE documents DROP COLUMN available_at")
    op.execute("ALTER TABLE documents DROP COLUMN attempt_count")
