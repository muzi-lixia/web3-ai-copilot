"""分片补来源页码。"""

from alembic import op

revision = "0009_chunk_pages"
down_revision = "0008_knowledge"
branch_labels = depends_on = None


def upgrade():
    # PDF 逐页切分，页码是分片能回溯到的位置；其他类型没有页码，留空。
    op.execute("ALTER TABLE chunks ADD COLUMN page_start integer")
    op.execute("ALTER TABLE chunks ADD COLUMN page_end integer")


def downgrade():
    op.execute("ALTER TABLE chunks DROP COLUMN page_end")
    op.execute("ALTER TABLE chunks DROP COLUMN page_start")
