"""受限 TTL 清理函数：服务账号不能扫描其他服务业务表。"""

from alembic import op

revision = "0005_retention"
down_revision = "0004_model_selection"
branch_labels = depends_on = None


def upgrade():
    op.execute("CREATE POLICY maintenance ON asset_results TO app_ddl USING(true) WITH CHECK(true)")
    op.execute("CREATE POLICY maintenance ON agent_conversations TO app_ddl USING(true) WITH CHECK(true)")
    op.execute("CREATE POLICY maintenance ON agent_result_refs TO app_ddl USING(true) WITH CHECK(true)")
    op.execute("""CREATE FUNCTION cleanup_asset_results() RETURNS void LANGUAGE sql SECURITY DEFINER
        SET search_path=public,pg_temp AS $$ DELETE FROM asset_results WHERE expires_at<now(); $$""")
    op.execute("REVOKE ALL ON FUNCTION cleanup_asset_results() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION cleanup_asset_results() TO app_rw, app_foundation")
    op.execute("""CREATE FUNCTION expired_agent_conversations() RETURNS TABLE(id text,user_id text)
        LANGUAGE sql SECURITY DEFINER SET search_path=public,pg_temp AS $$
        SELECT c.id,c.user_id FROM agent_conversations c WHERE c.expires_at<now()
        AND NOT EXISTS(SELECT 1 FROM agent_turns t WHERE t.conversation_id=c.id
        AND t.state IN ('queued','running')); $$""")
    op.execute("REVOKE ALL ON FUNCTION expired_agent_conversations() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION expired_agent_conversations() TO app_rw, app_agent")


def downgrade():
    op.execute("DROP FUNCTION expired_agent_conversations()")
    op.execute("DROP FUNCTION cleanup_asset_results()")
