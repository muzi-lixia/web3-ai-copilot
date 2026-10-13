"""知识库文档与切片。原件留在磁盘，这里只存元数据与向量分片。"""

from alembic import op

revision = "0008_knowledge"
down_revision = "0007_tool_trace"
branch_labels = depends_on = None


def upgrade():
    # 知识库是公共内容：两张表都不带 user_address，也不启用行级安全。
    # sha256 唯一约束让同一份文件重复上传时命中已有行，而不是叠出第二份向量。
    op.execute("""
        CREATE TABLE documents (
            id text PRIMARY KEY,
            title text NOT NULL,
            filename text NOT NULL,
            mime text NOT NULL,
            size_bytes bigint NOT NULL,
            sha256 text NOT NULL UNIQUE,
            status text NOT NULL DEFAULT 'pending',
            chunk_count integer NOT NULL DEFAULT 0,
            error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT documents_status_check
                CHECK (status IN ('pending', 'processing', 'ready', 'failed'))
        )
    """)
    # 向量维度与配置 knowledge_embedding_dimensions 绑定，改一处必须重建这一列。
    # 分片以 (文档, 序号) 为主键：切分顺序本身就是标识，不必再造自增列。
    op.execute("""
        CREATE TABLE chunks (
            document_id text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            ordinal integer NOT NULL,
            heading text,
            content text NOT NULL,
            embedding vector(1024) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (document_id, ordinal)
        )
    """)
    # 检索按余弦相似度取 top-k，索引与表一起建好，避免入库后再重建。
    op.execute("CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)")
    # 运行账号走 app_rw（默认表权限已覆盖）；app_foundation 是部署期的独立账号，需显式授权。
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON documents, chunks TO app_foundation")


def downgrade():
    op.execute("DROP TABLE chunks")
    op.execute("DROP TABLE documents")
