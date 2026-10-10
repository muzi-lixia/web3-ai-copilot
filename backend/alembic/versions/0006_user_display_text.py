"""分离用户展示原文与模型安全输入；旧数据不伪造原文。"""

from alembic import op

revision = "0006_user_display_text"
down_revision = "0005_retention"
branch_labels = depends_on = None


def upgrade():
    # query 仍是安全模型输入。展示原文沿用轮次 RLS、会话级联删除和 TTL。
    # 旧数据没有原文，允许 NULL，由读取层回退，不能反向编造用户输入。
    op.execute("ALTER TABLE agent_turns ADD COLUMN display_text text")
    op.execute("COMMENT ON COLUMN agent_turns.display_text IS '用户原文，仅供本人展示；禁止进入模型和日志'")


def downgrade():
    op.execute("ALTER TABLE agent_turns DROP COLUMN display_text")
