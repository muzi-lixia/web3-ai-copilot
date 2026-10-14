"""知识库任务执行租约及防旧执行者覆盖的领取 token。"""

from alembic import op

revision = "0012_knowledge_lease"
down_revision = "0011_document_failure"
branch_labels = depends_on = None


def upgrade():
    op.execute("ALTER TABLE documents ADD COLUMN worker_id text")
    op.execute("ALTER TABLE documents ADD COLUMN claim_token text")
    op.execute("ALTER TABLE documents ADD COLUMN lease_until timestamptz")
    # 升级须停止旧消费者：旧任务没有租约，安全退回队列交给新消费者。
    op.execute("UPDATE documents SET status='pending' WHERE status='processing'")
    op.execute("""ALTER TABLE documents ADD CONSTRAINT documents_lease_check CHECK (
        (status='processing' AND worker_id IS NOT NULL AND claim_token IS NOT NULL
            AND lease_until IS NOT NULL)
        OR (status<>'processing' AND worker_id IS NULL AND claim_token IS NULL AND lease_until IS NULL))""")
    op.execute("CREATE INDEX documents_processing_lease ON documents(lease_until) WHERE status='processing'")


def downgrade():
    op.execute("DROP INDEX documents_processing_lease")
    op.execute("ALTER TABLE documents DROP CONSTRAINT documents_lease_check")
    op.execute("ALTER TABLE documents DROP COLUMN lease_until")
    op.execute("ALTER TABLE documents DROP COLUMN claim_token")
    op.execute("ALTER TABLE documents DROP COLUMN worker_id")
