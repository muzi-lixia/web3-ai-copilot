"""记录每轮的工具调用轨迹，供排查与回归使用。"""

from alembic import op

revision = "0007_tool_trace"
down_revision = "0006_user_display_text"
branch_labels = depends_on = None


def upgrade():
    # 轨迹描述「模型调了哪些工具、传了什么参数、耗时多少」，不含身份也不含结果数值。
    # 带默认值让存量行自动填 []，读取层无需区分 NULL 与空列表。
    op.execute("ALTER TABLE agent_turns ADD COLUMN tool_trace jsonb NOT NULL DEFAULT '[]'::jsonb")
    op.execute(
        "COMMENT ON COLUMN agent_turns.tool_trace IS "
        "'工具调用轨迹（工具/参数/耗时/状态）：不进模型的白名单，"
        "随展示快照以 tool_meta 下发前端；不含身份与结果数值'"
    )


def downgrade():
    op.execute("ALTER TABLE agent_turns DROP COLUMN tool_trace")
