"""知识库文档元数据的持久化。原件留在磁盘，向量分片由后续的入库步骤写入。"""

from sqlalchemy import text

# 列表与详情共用同一组字段：分片内容与向量不随元数据一起返回。
_COLUMNS = """id, title, filename, mime, size_bytes, status,
    chunk_count, stage, error_code, error, created_at, updated_at"""

# 向量以字面量文本传入，由 SQL 侧转换类型，省掉一个客户端依赖。
_INSERT_CHUNK = text("""INSERT INTO chunks(document_id, ordinal, heading, content,
        page_start, page_end, kind, embedding)
    VALUES(:document_id, :ordinal, :heading, :content, :page_start, :page_end, :kind,
        CAST(:embedding AS vector))""")

# 按余弦距离取最近邻：距离越小越接近，1 减距离即余弦相似度。
# 两个占位符都写成 CAST(... AS vector)，与写入同一条路子，不在客户端拼类型；
# ORDER BY 与 LIMIT 同句，才用得上 chunks_embedding_hnsw 这条索引。
_SEARCH = text("""SELECT c.document_id, c.ordinal, c.heading, c.content,
        c.page_start, c.page_end, c.kind, d.title, d.filename,
        (1 - (c.embedding <=> CAST(:q AS vector)))::float8 AS score
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    ORDER BY c.embedding <=> CAST(:q AS vector)
    LIMIT :k""")


class KnowledgeRepository:
    """documents 表的读写。知识库是公共内容，事务里不设置用户身份。"""

    def __init__(self, database):
        """持有数据库连接层，本身不缓存文档内容。"""
        self.db = database

    async def create(self, document: dict) -> None:
        """登记一条待处理文档；重复内容的判定由调用方先查摘要完成。"""
        async with self.db.sessions.begin() as s:
            await s.execute(
                text("""INSERT INTO documents(id,title,filename,mime,size_bytes,sha256,status)
                VALUES(:id,:title,:filename,:mime,:size,:sha256,'pending')"""),
                document,
            )

    async def find_by_hash(self, digest: str) -> str | None:
        """按内容摘要找已存在的文档 id，让重复上传命中已有行而不是叠出第二份向量。"""
        async with self.db.sessions.begin() as s:
            return await s.scalar(text("SELECT id FROM documents WHERE sha256=:d"), dict(d=digest))

    async def listing(self) -> list[dict]:
        """按上传时间倒序列出全部文档元数据。"""
        async with self.db.sessions.begin() as s:
            rows = (
                (await s.execute(text(f"SELECT {_COLUMNS} FROM documents ORDER BY created_at DESC")))
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def read(self, document_id: str) -> dict | None:
        """按 id 读单条文档元数据，不存在返回 None。"""
        async with self.db.sessions.begin() as s:
            row = (
                (await s.execute(text(f"SELECT {_COLUMNS} FROM documents WHERE id=:i"), dict(i=document_id)))
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def search(self, embedding: str, limit: int) -> list[dict]:
        """按余弦相似度取最近的分片，连出处一起返回。

        入参是 pgvector 字面量文本，由 SQL 侧转类型。相似度只用于展示与取舍，
        固定 4 位小数，不参与后续计算。
        """
        async with self.db.sessions.begin() as s:
            rows = (await s.execute(_SEARCH, dict(q=embedding, k=limit))).mappings().all()
        return [dict(row, score=round(row["score"], 4)) for row in rows]

    async def delete(self, document_id: str) -> bool:
        """删除文档行，分片随外键级联删除；返回是否真的删掉了。"""
        async with self.db.sessions.begin() as s:
            result = await s.execute(text("DELETE FROM documents WHERE id=:i"), dict(i=document_id))
            return result.rowcount > 0

    async def claim(self) -> dict | None:
        """取一条待处理文档并置为处理中，没有则返回 None。

        取单与改状态合在一条语句里，并用 SKIP LOCKED 跳过别人已锁住的行：多个进程
        同时轮询时不会重复处理同一篇。
        """
        async with self.db.sessions.begin() as s:
            row = (
                (
                    await s.execute(
                        text("""UPDATE documents SET status='processing', updated_at=now()
                    WHERE id = (
                        SELECT id FROM documents WHERE status='pending'
                        ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id, filename""")
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def complete(self, document_id: str, chunks: list[dict]) -> None:
        """在一个事务里替换分片并置为可检索。

        先删旧分片：处理中断过或重复触发时不会留下两代内容。状态与分片同事务写入，
        避免出现「分片已就绪但状态还停在处理中」。
        """
        async with self.db.sessions.begin() as s:
            await s.execute(text("DELETE FROM chunks WHERE document_id=:i"), dict(i=document_id))
            if chunks:
                await s.execute(
                    _INSERT_CHUNK,
                    [dict(chunk, document_id=document_id) for chunk in chunks],
                )
            await s.execute(
                text("""UPDATE documents SET status='ready', chunk_count=:n, error=NULL,
                    stage=NULL, error_code=NULL, updated_at=now() WHERE id=:i"""),
                dict(n=len(chunks), i=document_id),
            )

    async def fail(self, document_id: str, reason: str, *, stage: str, error_code: str) -> None:
        """记录失败阶段、错误码与原因并置为失败，同时清掉分片，避免半成品被检索到。"""
        async with self.db.sessions.begin() as s:
            await s.execute(text("DELETE FROM chunks WHERE document_id=:i"), dict(i=document_id))
            await s.execute(
                text("""UPDATE documents SET status='failed', chunk_count=0, error=:e, stage=:stage,
                    error_code=:code, updated_at=now() WHERE id=:i"""),
                dict(e=reason[:2000], i=document_id, stage=stage, code=error_code),
            )

    async def retry(self, document_id: str) -> bool:
        """把失败文档退回待处理并清掉上次的失败记录，返回是否真的改了状态。

        状态判定写在语句里：只有当前是失败的文档才改得动，已就绪的文档重新排队会
        白白重建一遍分片。
        """
        async with self.db.sessions.begin() as s:
            result = await s.execute(
                text("""UPDATE documents SET status='pending', error=NULL, stage=NULL,
                    error_code=NULL, updated_at=now() WHERE id=:i AND status='failed'"""),
                dict(i=document_id),
            )
            return result.rowcount > 0

    async def retry_stuck(self) -> int:
        """把上次进程中断留下的「处理中」退回待处理，返回退回条数。

        处理是幂等的（写入前先清空旧分片），重跑一次比留下永远处理不了的行安全。
        """
        async with self.db.sessions.begin() as s:
            result = await s.execute(
                text("UPDATE documents SET status='pending', updated_at=now() WHERE status='processing'")
            )
            return result.rowcount
