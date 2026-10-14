"""记录文档与分片的向量空间版本；旧数据不猜测版本，须从原件重建。"""

from alembic import op

revision = "0013_embedding_version"
down_revision = "0012_knowledge_lease"
branch_labels = depends_on = None


def upgrade():
    op.execute("ALTER TABLE documents ADD COLUMN embedding_version text")
    op.execute("ALTER TABLE chunks ADD COLUMN embedding_version text")
    op.execute(
        "CREATE INDEX documents_embedding_version ON documents(embedding_version) WHERE status='ready'"
    )


def downgrade():
    op.execute("DROP INDEX documents_embedding_version")
    op.execute("ALTER TABLE chunks DROP COLUMN embedding_version")
    op.execute("ALTER TABLE documents DROP COLUMN embedding_version")
