"""关联标识、checkpoint 清理意图与模型运行 ID。"""

from sqlalchemy import text

from alembic import op

revision = "0015_observability"
down_revision = "0014_runtime_operations"
branch_labels = depends_on = None


def upgrade():
    connection = op.get_bind()
    schema = "public" if op.get_context().as_sql else connection.scalar(text("SELECT current_schema()"))
    schema = connection.dialect.identifier_preparer.quote_schema(schema)
    op.execute("ALTER TABLE documents ADD COLUMN trace_id text NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE agent_turns ADD COLUMN run_id text")
    op.execute("""CREATE TABLE agent_checkpoint_deletions (
        thread_id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute(f"""CREATE FUNCTION queue_checkpoint_deletion() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
        INSERT INTO {schema}.agent_checkpoint_deletions(thread_id) VALUES(OLD.id) ON CONFLICT DO NOTHING;
        RETURN OLD; END $$""")
    op.execute("""CREATE TRIGGER agent_checkpoint_cleanup AFTER DELETE ON agent_conversations
        FOR EACH ROW EXECUTE FUNCTION queue_checkpoint_deletion()""")
    op.execute("REVOKE ALL ON FUNCTION queue_checkpoint_deletion() FROM PUBLIC")
    op.execute("GRANT SELECT,INSERT,DELETE ON agent_checkpoint_deletions TO app_agent")

    op.execute(f"""CREATE FUNCTION agent_observability_status() RETURNS TABLE(
        total bigint, failed bigint, oldest_active_seconds float8, checkpoint_pending bigint)
        LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        SELECT count(*) FILTER(WHERE created_at>now()-interval '15 minutes'),
            count(*) FILTER(WHERE state='failed' AND created_at>now()-interval '15 minutes'),
            COALESCE(max(EXTRACT(EPOCH FROM clock_timestamp()-created_at))
                FILTER(WHERE state IN ('queued','running')),0)::float8,
            (SELECT count(*) FROM {schema}.agent_checkpoint_deletions)
        FROM {schema}.agent_turns $$""")
    op.execute("REVOKE ALL ON FUNCTION agent_observability_status() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION agent_observability_status() TO app_agent")


def downgrade():
    op.execute("DROP FUNCTION agent_observability_status()")
    op.execute("DROP TRIGGER agent_checkpoint_cleanup ON agent_conversations")
    op.execute("DROP FUNCTION queue_checkpoint_deletion()")
    op.execute("DROP TABLE agent_checkpoint_deletions")
    op.execute("ALTER TABLE agent_turns DROP COLUMN run_id")
    op.execute("ALTER TABLE documents DROP COLUMN trace_id")
