"""分片补块类型。"""

from alembic import op

revision = "0010_chunk_kind"
down_revision = "0009_chunk_pages"
branch_labels = depends_on = None


def upgrade():
    # 块类型（段落 / 标题 / 表格 / 代码）随分片一起落库，检索结果按类型分别呈现。
    op.execute("ALTER TABLE chunks ADD COLUMN kind text NOT NULL DEFAULT 'paragraph'")


def downgrade():
    op.execute("ALTER TABLE chunks DROP COLUMN kind")
