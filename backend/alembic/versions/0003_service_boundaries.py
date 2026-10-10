"""独立认证、结果与轻量对话表；保留既有表作为只读历史，不破坏原数据。"""

from alembic import op

revision = "0003_service_boundaries"
down_revision = "0002_retry_integrity"
branch_labels = depends_on = None


def execute_batch(sql):
    """asyncpg 不接受一个 prepare 内的多条语句；保留 $$ 函数体内部的分号。"""
    inside = False
    start = 0
    i = 0
    while i < len(sql):
        if sql[i:i+2] == '$$':
            inside = not inside
            i += 2
            continue
        if sql[i] == ';' and not inside:
            statement = sql[start:i].strip()
            if statement:
                op.execute(statement)
            start = i + 1
        i += 1
    if sql[start:].strip():
        op.execute(sql[start:])


def upgrade():
    # 认证表只由基础服务使用。登录用户使用稳定随机 ID，不再以钱包地址作为对话归属键。
    execute_batch("""
    CREATE TABLE service_users (id text PRIMARY KEY, address text UNIQUE NOT NULL);
    CREATE TABLE auth_challenges (
        nonce text PRIMARY KEY, address text UNIQUE NOT NULL, message text NOT NULL,
        expires_at timestamptz NOT NULL);
    CREATE TABLE login_sessions (
        id text PRIMARY KEY, user_id text NOT NULL REFERENCES service_users(id),
        expires_at timestamptz NOT NULL, revoked boolean NOT NULL DEFAULT false);
    CREATE TABLE refresh_credentials (
        digest text PRIMARY KEY, session_id text NOT NULL REFERENCES login_sessions(id),
        consumed boolean NOT NULL DEFAULT false);
    CREATE INDEX refresh_session_idx ON refresh_credentials(session_id);
    CREATE TABLE request_limits (
        bucket text NOT NULL, bucket_window bigint NOT NULL, count integer NOT NULL,
        PRIMARY KEY(bucket, bucket_window));
    CREATE TABLE security_audit (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id text, event text NOT NULL, status text NOT NULL, trace_id text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now());
    CREATE TABLE asset_results (
        id text PRIMARY KEY, user_id text NOT NULL REFERENCES service_users(id),
        payload jsonb NOT NULL, expires_at timestamptz NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now());
    CREATE INDEX result_expiry_idx ON asset_results(expires_at);
    CREATE TABLE agent_conversations (
        id text PRIMARY KEY, user_id text UNIQUE NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL);
    CREATE TABLE agent_turns (
        id text PRIMARY KEY,
        conversation_id text NOT NULL REFERENCES agent_conversations(id) ON DELETE CASCADE,
        user_id text NOT NULL, client_id text NOT NULL, query text NOT NULL,
        answer text NOT NULL DEFAULT '', state text NOT NULL DEFAULT 'queued',
        error_code text, version integer NOT NULL DEFAULT 0,
        results jsonb NOT NULL DEFAULT '[]', usage jsonb NOT NULL DEFAULT '{}',
        trace_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(conversation_id, client_id),
        CHECK(state IN ('queued','running','completed','failed','cancelled')));
    CREATE UNIQUE INDEX agent_one_active_turn ON agent_turns(conversation_id)
        WHERE state IN ('queued','running');
    CREATE TABLE agent_result_refs (
        result_id text PRIMARY KEY, user_id text NOT NULL,
        conversation_id text NOT NULL REFERENCES agent_conversations(id) ON DELETE CASCADE);
    """)
    # 事务设置 app.current_user；新对话表同时用 RLS 和显式归属条件防止串用户。
    for table in ("agent_conversations", "agent_turns", "agent_result_refs", "asset_results"):
        execute_batch(f"""ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY owner_only ON {table}
        USING (user_id = current_setting('app.current_user', true))
        WITH CHECK (user_id = current_setting('app.current_user', true));""")
    # 仅持有部署租约的 Agent 会调用启动恢复；函数不接受用户输入，运行账号不能 DDL。
    execute_batch("""CREATE FUNCTION agent_recover_turns() RETURNS void LANGUAGE sql SECURITY DEFINER
    SET search_path=public,pg_temp AS $$
    UPDATE agent_turns SET state='failed', error_code='service_restarted', version=version+1
    WHERE state IN ('queued','running'); $$;
    REVOKE ALL ON FUNCTION agent_recover_turns() FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION agent_recover_turns() TO app_rw;
    GRANT USAGE, SELECT ON SEQUENCE security_audit_id_seq TO app_rw;
    """)
    # FORCE RLS 下函数所有者也需要合法身份策略；恢复函数通过专用迁移角色执行。
    execute_batch("""CREATE POLICY maintenance ON agent_turns TO app_ddl USING(true) WITH CHECK(true);""")


def downgrade():
    raise RuntimeError("本迁移包含用户与结果数据，不提供破坏性自动回退；请从备份恢复。")
