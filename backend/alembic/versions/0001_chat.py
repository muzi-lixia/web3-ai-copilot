"""建立会话、轮次、消息和摘要四张表，并配置 RLS 与受限的重启恢复函数。

DDL 已冻结在此版本中；不能导入实时 ORM 模型建表，否则未来修改模型会改变旧迁移的含义。
app_rw 只能访问当前用户的行；app_ddl 有显式维护策略，用于迁移和恢复函数。
降级会删除此版本的表，只用于开发/测试，不应作为生产数据回滚手段。
"""

from alembic import op

revision = "0001_chat"
down_revision = None
branch_labels = None
depends_on = None

TABLES = ("chat_sessions", "chat_turns", "chat_messages", "chat_summaries")

# 冻结的迁移 SQL：未来 ORM 改动不能改变已发布版本的建表结果。
DDL = (
    """
CREATE TABLE chat_sessions (
	id UUID NOT NULL,
	user_address VARCHAR(42) NOT NULL,
	title VARCHAR(80) NOT NULL,
	next_seq INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (id, user_address),
	CHECK (user_address = lower(user_address))
)
""",
    """CREATE INDEX chat_sessions_owner_updated ON chat_sessions (user_address, updated_at)""",
    """
CREATE TABLE chat_summaries (
	id UUID NOT NULL,
	session_id UUID NOT NULL,
	user_address VARCHAR(42) NOT NULL,
	version INTEGER NOT NULL,
	covered_through_seq INTEGER NOT NULL,
	content JSON NOT NULL,
	source_message_ids JSON NOT NULL,
	model VARCHAR(100) NOT NULL,
	prompt_version VARCHAR(30) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (session_id, version),
	FOREIGN KEY(session_id, user_address) REFERENCES chat_sessions (id, user_address) ON DELETE CASCADE
)
""",
    """
CREATE TABLE chat_turns (
	id UUID NOT NULL,
	session_id UUID NOT NULL,
	user_address VARCHAR(42) NOT NULL,
	client_msg_id UUID NOT NULL,
	prompt_message_id UUID NOT NULL,
	retry_of UUID,
	status VARCHAR(20) NOT NULL,
	model VARCHAR(100) NOT NULL,
	version INTEGER NOT NULL,
	summary_version INTEGER,
	context_info JSON NOT NULL,
	usage JSON NOT NULL,
	error TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	finished_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (id, session_id, user_address),
	UNIQUE (session_id, client_msg_id),
        FOREIGN KEY(session_id, user_address) REFERENCES chat_sessions (id, user_address) ON DELETE
CASCADE,
	CHECK (status IN ('running','completed','truncated','failed','cancelled','interrupted'))
)
""",
    """CREATE UNIQUE INDEX chat_one_active_turn ON chat_turns (session_id) WHERE status='running'""",
    """
CREATE TABLE chat_messages (
	id UUID NOT NULL,
	session_id UUID NOT NULL,
	turn_id UUID NOT NULL,
	user_address VARCHAR(42) NOT NULL,
	seq INTEGER NOT NULL,
	role VARCHAR(20) NOT NULL,
	content TEXT NOT NULL,
	status VARCHAR(20) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (id, session_id, user_address),
	UNIQUE (session_id, seq),
	UNIQUE (turn_id, role),
        FOREIGN KEY(turn_id, session_id, user_address) REFERENCES chat_turns (id, session_id,
user_address) ON DELETE CASCADE,
	CHECK (role IN ('user','assistant'))
)
""",
    """ALTER TABLE chat_turns ADD CONSTRAINT chat_turn_prompt_fk FOREIGN KEY(prompt_message_id, session_id,
user_address) REFERENCES chat_messages (id, session_id, user_address) DEFERRABLE INITIALLY DEFERRED""",
)


def upgrade():
    """按冻结 DDL 建表，再配置运行时隔离策略、维护策略和受限恢复函数。"""
    for statement in DDL:
        op.execute(statement)
    for table in TABLES:
        # ENABLE 启用行隔离；FORCE 使表所有者也受策略约束，运行角色不能绕过 RLS。
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # USING 限制可见行，WITH CHECK 限制写入后行归属，二者缺一不可。
        op.execute(f"""CREATE POLICY {table}_owner ON {table} TO app_rw
            USING (user_address = current_setting('app.current_user', true))
            WITH CHECK (user_address = current_setting('app.current_user', true))""")
        # 迁移角色获得显式维护策略；app_rw 不属于该角色，不能 SET ROLE 冒用维护权限。
        op.execute(f"CREATE POLICY {table}_maintenance ON {table} TO app_ddl USING (true) WITH CHECK (true)")
    # 只开放固定、无参数的跨用户恢复动作，不开放任意查询。
    # SECURITY DEFINER 使用维护身份；固定 search_path 防止同名对象替换。
    # 调用方必须先持运行锁，否则可能误标另一个实例的活动生成。
    op.execute("""CREATE FUNCTION chat_recover_interrupted() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        BEGIN
          UPDATE public.chat_messages SET status='interrupted'
          WHERE role='assistant' AND turn_id IN
            (SELECT id FROM public.chat_turns WHERE status='running');
          UPDATE public.chat_turns SET status='interrupted', error='服务重启，回复已中断',
              version=version+1, finished_at=now() WHERE status='running';
        END $$""")
    # 回收 PUBLIC 默认执行权，再仅向应用开放这一个无参数恢复入口。
    op.execute("REVOKE ALL ON FUNCTION chat_recover_interrupted() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION chat_recover_interrupted() TO app_rw")


def downgrade():
    """先删除恢复函数和循环外键，再按依赖顺序删表；此操作会删除会话数据。"""
    op.execute("DROP FUNCTION IF EXISTS chat_recover_interrupted()")
    op.execute("ALTER TABLE chat_turns DROP CONSTRAINT chat_turn_prompt_fk")
    for table in ("chat_messages", "chat_turns", "chat_summaries", "chat_sessions"):
        op.execute(f"DROP TABLE {table}")
