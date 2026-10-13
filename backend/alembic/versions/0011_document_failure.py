"""文档失败定位字段。"""

from alembic import op

revision = "0011_document_failure"
down_revision = "0010_chunk_kind"
branch_labels = depends_on = None


def upgrade():
    # 失败分两列记：stage 指出炸在离线链路哪一步，error_code 是可供日志聚合的受控枚举，
    # error 仍留人类可读的原因。
    op.execute("ALTER TABLE documents ADD COLUMN stage text")
    op.execute("ALTER TABLE documents ADD COLUMN error_code text")


def downgrade():
    op.execute("ALTER TABLE documents DROP COLUMN stage")
    op.execute("ALTER TABLE documents DROP COLUMN error_code")
