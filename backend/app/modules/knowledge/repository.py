"""知识库文档元数据的持久化。原件留在磁盘，向量分片由后续的入库步骤写入。"""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text

from app.core.exceptions import ConflictError
from app.core.logging import trace_id

# 列表与详情共用同一组字段：分片内容与向量不随元数据一起返回。
_COLUMNS = """id, title, filename, mime, size_bytes, status,
    chunk_count, embedding_version, attempt_count, stage, error_code, error, created_at, updated_at"""

# 向量以字面量文本传入，由 SQL 侧转换类型，省掉一个客户端依赖。
_INSERT_CHUNK = text("""INSERT INTO chunks(document_id, ordinal, heading, content,
        page_start, page_end, kind, embedding, embedding_version)
    VALUES(:document_id, :ordinal, :heading, :content, :page_start, :page_end, :kind,
        CAST(:embedding AS vector), :embedding_version)""")

# 按余弦距离取最近邻：距离越小越接近，1 减距离即余弦相似度。
# 两个占位符都写成 CAST(... AS vector)，与写入同一条路子，不在客户端拼类型；
# ORDER BY 与 LIMIT 同句，才用得上 chunks_embedding_hnsw 这条索引。
_SEARCH = text("""SELECT c.document_id, c.ordinal, c.heading, c.content,
        c.page_start, c.page_end, c.kind, d.title, d.filename,
        (1 - (c.embedding <=> CAST(:q AS vector)))::float8 AS score
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE d.status='ready' AND d.embedding_version=:version AND c.embedding_version=:version
    ORDER BY c.embedding <=> CAST(:q AS vector)
    LIMIT :k""")


@dataclass
class CleanupAttempt:
    suffix: str
    failed: bool = False


class KnowledgeRepository:
    """documents 表的读写。知识库是公共内容，事务里不设置用户身份。"""

    def __init__(self, database):
        """持有数据库连接层，本身不缓存文档内容。"""
        self.db = database

    async def create(self, document: dict) -> bool:
        """登记一条待处理文档；重复内容的判定由调用方先查摘要完成。"""
        async with self.db.sessions.begin() as s:
            # 与文件清理互斥：清理先完成时拒绝登记，不能创建指向已删除文件的记录。
            intent = await s.scalar(
                text("SELECT document_id FROM knowledge_file_deletions WHERE document_id=:id FOR UPDATE"),
                dict(id=document["id"]),
            )
            if intent is None:
                raise ConflictError("上传登记已过期，请重试", code="document_upload_expired")
            result = await s.execute(
                text("""INSERT INTO documents(id,title,filename,mime,size_bytes,sha256,status,trace_id)
                VALUES(:id,:title,:filename,:mime,:size,:sha256,'pending',:trace)
                ON CONFLICT (sha256) DO NOTHING"""),
                {**document, "trace": trace_id.get()},
            )
            if result.rowcount:
                await s.execute(
                    text("DELETE FROM knowledge_file_deletions WHERE document_id=:id"),
                    dict(id=document["id"]),
                )
            return result.rowcount > 0

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

    async def search(self, embedding: str, limit: int, version: str) -> list[dict]:
        """按余弦相似度取最近的分片，连出处一起返回。

        入参是 pgvector 字面量文本，由 SQL 侧转类型。相似度只用于展示与取舍，
        保留原始精度供服务层阈值过滤，不在过滤前四舍五入。
        """
        async with self.db.sessions.begin() as s:
            rows = (await s.execute(_SEARCH, dict(q=embedding, k=limit, version=version))).mappings().all()
        return [dict(row) for row in rows]

    async def index_status(self, version: str) -> dict:
        """区分无语料与版本不兼容，避免把需要重建索引误报成普通无答案。"""
        async with self.db.sessions() as session:
            row = (
                (
                    await session.execute(
                        text("""
                SELECT count(*) AS ready,
                    count(*) FILTER (WHERE embedding_version=:v) AS compatible
                FROM documents WHERE status='ready'
            """),
                        dict(v=version),
                    )
                )
                .mappings()
                .one()
            )
        return dict(row)

    async def reindex(self, document_id: str) -> bool:
        """重新排队就绪/失败文档，同事务移除旧分片；执行中的任务须先结束。"""
        async with self.db.sessions.begin() as session:
            result = await session.execute(
                text("""
                UPDATE documents SET status='pending', chunk_count=0, embedding_version=NULL,
                    error=NULL, stage=NULL, error_code=NULL, attempt_count=0,
                    available_at=clock_timestamp(), updated_at=clock_timestamp()
                WHERE id=:id AND status IN ('ready','failed')
            """),
                dict(id=document_id),
            )
            if not result.rowcount:
                return False
            await session.execute(text("DELETE FROM chunks WHERE document_id=:id"), dict(id=document_id))
            return True

    async def schedule_cleanup(self, document_id: str, suffix: str, delay: float = 0) -> None:
        """上传前登记延迟清理意图；即使数据库提交结果不确定，也不直接删除原件。"""
        async with self.db.sessions.begin() as session:
            await session.execute(
                text("""INSERT INTO knowledge_file_deletions(document_id,suffix,available_at)
                VALUES(:id,:suffix,clock_timestamp()+:delay*interval '1 second')
                ON CONFLICT(document_id) DO UPDATE SET available_at=EXCLUDED.available_at"""),
                dict(id=document_id, suffix=suffix, delay=delay),
            )

    async def cleanup_candidates(self, limit=5) -> list[dict]:
        async with self.db.sessions() as session:
            rows = (
                (
                    await session.execute(
                        text("""SELECT f.document_id,f.suffix FROM knowledge_file_deletions f
                WHERE f.available_at<=clock_timestamp()
                    AND NOT EXISTS(SELECT 1 FROM documents d WHERE d.id=f.document_id)
                ORDER BY f.available_at LIMIT :limit"""),
                        dict(limit=limit),
                    )
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]

    @asynccontextmanager
    async def cleanup_guard(self, document_id):
        """锁住清理意图并复查引用；与上传登记串行化，避免检查后出现新文档。"""
        async with self.db.sessions.begin() as session:
            suffix = await session.scalar(
                text("SELECT suffix FROM knowledge_file_deletions WHERE document_id=:id FOR UPDATE"),
                dict(id=document_id),
            )
            referenced = await session.scalar(
                text("SELECT id FROM documents WHERE id=:id"), dict(id=document_id)
            )
            if suffix is None or referenced:
                yield None
                return
            attempt = CleanupAttempt(suffix)
            yield attempt
            if attempt.failed:
                await session.execute(
                    text("""UPDATE knowledge_file_deletions SET attempts=attempts+1,
                    available_at=clock_timestamp()+interval '30 seconds' WHERE document_id=:id"""),
                    dict(id=document_id),
                )
            else:
                await session.execute(
                    text("DELETE FROM knowledge_file_deletions WHERE document_id=:id"), dict(id=document_id)
                )

    async def finish_cleanup(self, document_id: str, *, failed=False) -> None:
        async with self.db.sessions.begin() as session:
            if failed:
                await session.execute(
                    text("""UPDATE knowledge_file_deletions SET attempts=attempts+1,
                    available_at=clock_timestamp()
                        +LEAST(3600,power(2,LEAST(attempts+1,12)))*interval '1 second'
                    WHERE document_id=:id"""),
                    dict(id=document_id),
                )
            else:
                await session.execute(
                    text("DELETE FROM knowledge_file_deletions WHERE document_id=:id"), dict(id=document_id)
                )

    async def delete(self, document_id: str) -> bool:
        """逻辑删除与原件清理任务同事务；磁盘删除失败仍有可重试记录。"""
        async with self.db.sessions.begin() as session:
            filename = await session.scalar(
                text("DELETE FROM documents WHERE id=:id RETURNING filename"), dict(id=document_id)
            )
            if filename is None:
                return False
            await session.execute(
                text("""INSERT INTO knowledge_file_deletions(document_id,suffix)
                VALUES(:id,:suffix) ON CONFLICT(document_id) DO NOTHING"""),
                dict(id=document_id, suffix=Path(filename).suffix.lower()),
            )
            return True

    async def claim(self, worker_id: str, claim_token: str, lease_seconds: float) -> dict | None:
        """原子领取待处理任务，每次执行分配新 token，防止旧执行者续租或提交。"""
        async with self.db.sessions.begin() as session:
            row = (
                (
                    await session.execute(
                        text("""
                UPDATE documents SET status='processing', worker_id=:worker,
                    claim_token=:token, attempt_count=attempt_count+1,
                    lease_until=clock_timestamp() + :seconds * interval '1 second',
                    updated_at=clock_timestamp()
                WHERE id=(SELECT id FROM documents WHERE status='pending' AND available_at<=clock_timestamp()
                    ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED)
                RETURNING id, filename, trace_id, attempt_count
            """),
                        dict(worker=worker_id, token=claim_token, seconds=lease_seconds),
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def renew(self, document_id: str, claim_token: str, lease_seconds: float) -> bool:
        """只允许当前、尚未过期的执行续租；过期后即使尚未接管也不能复活。"""
        async with self.db.sessions.begin() as session:
            result = await session.execute(
                text("""
                UPDATE documents SET lease_until=clock_timestamp() + :seconds * interval '1 second'
                WHERE id=:id AND claim_token=:token AND status='processing'
                    AND lease_until > clock_timestamp()
            """),
                dict(id=document_id, token=claim_token, seconds=lease_seconds),
            )
            return result.rowcount > 0

    async def complete(self, document_id: str, claim_token: str, chunks: list[dict]) -> bool:
        """先校验租约并锁住文档，再同事务替换分片；旧执行者不能改动任何分片。"""
        versions = {chunk["embedding_version"] for chunk in chunks}
        if len(versions) != 1 or not next(iter(versions)):
            raise ValueError("分片必须属于同一个明确的向量版本")
        version = next(iter(versions))
        async with self.db.sessions.begin() as session:
            result = await session.execute(
                text("""
                UPDATE documents SET status='ready', chunk_count=:n, embedding_version=:version, error=NULL,
                    stage=NULL, error_code=NULL, worker_id=NULL, claim_token=NULL,
                    lease_until=NULL, updated_at=clock_timestamp()
                WHERE id=:id AND status='processing' AND claim_token=:token
                    AND lease_until > clock_timestamp()
            """),
                dict(id=document_id, token=claim_token, n=len(chunks), version=version),
            )
            if not result.rowcount:
                return False
            await session.execute(text("DELETE FROM chunks WHERE document_id=:i"), dict(i=document_id))
            if chunks:
                await session.execute(
                    _INSERT_CHUNK, [dict(chunk, document_id=document_id) for chunk in chunks]
                )
            return True

    async def fail(
        self, document_id: str, claim_token: str, reason: str, *, stage: str, error_code: str
    ) -> bool:
        """失败提交也必须持有有效租约，避免旧任务清掉接管者生成的分片。"""
        async with self.db.sessions.begin() as session:
            result = await session.execute(
                text("""
                UPDATE documents SET status='failed', chunk_count=0, error=:e, stage=:stage,
                    error_code=:code, worker_id=NULL, claim_token=NULL, lease_until=NULL,
                    updated_at=clock_timestamp()
                WHERE id=:id AND status='processing' AND claim_token=:token
                    AND lease_until > clock_timestamp()
            """),
                dict(id=document_id, token=claim_token, e=reason[:2000], stage=stage, code=error_code),
            )
            if not result.rowcount:
                return False
            await session.execute(text("DELETE FROM chunks WHERE document_id=:i"), dict(i=document_id))
            return True

    async def retry(self, document_id: str) -> bool:
        """把失败文档退回待处理并清掉上次的失败记录，返回是否真的改了状态。

        状态判定写在语句里：只有当前是失败的文档才改得动，已就绪的文档重新排队会
        白白重建一遍分片。
        """
        async with self.db.sessions.begin() as s:
            result = await s.execute(
                text("""UPDATE documents SET status='pending', error=NULL, stage=NULL,
                    error_code=NULL, attempt_count=0, available_at=clock_timestamp(),
                    updated_at=now() WHERE id=:i AND status='failed'"""),
                dict(i=document_id),
            )
            return result.rowcount > 0

    async def retry_stuck(self, max_attempts=3, retry_delay=5) -> int:
        """仅回收过期租约；反复崩溃达到预算后转失败，其他任务退避后重试。"""
        async with self.db.sessions.begin() as session:
            result = await session.execute(
                text("""
                UPDATE documents SET status=CASE WHEN attempt_count>=:max THEN 'failed' ELSE 'pending' END,
                    error_code=CASE WHEN attempt_count>=:max THEN 'task_attempts_exhausted' ELSE NULL END,
                    stage=CASE WHEN attempt_count>=:max THEN 'runtime' ELSE NULL END,
                    error=CASE WHEN attempt_count>=:max THEN '任务反复中断，已停止自动重试' ELSE NULL END,
                    worker_id=NULL, claim_token=NULL, lease_until=NULL,
                    available_at=clock_timestamp()+:delay*interval '1 second', updated_at=clock_timestamp()
                WHERE id IN (SELECT id FROM documents
                    WHERE status='processing' AND lease_until<=clock_timestamp() FOR UPDATE SKIP LOCKED)
            """),
                dict(max=max_attempts, delay=retry_delay),
            )
            return result.rowcount

    async def heartbeat(self, worker_id: str) -> None:
        async with self.db.sessions.begin() as session:
            await session.execute(
                text("""INSERT INTO knowledge_workers(worker_id) VALUES(:id)
                ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at=clock_timestamp()"""),
                dict(id=worker_id),
            )
            await session.execute(
                text("DELETE FROM knowledge_workers WHERE heartbeat_at<now()-interval '1 day'")
            )

    async def queue_status(self) -> dict:
        async with self.db.sessions() as session:
            row = (
                (
                    await session.execute(
                        text("""SELECT
                count(*) FILTER(WHERE status='pending') AS pending,
                count(*) FILTER(WHERE status='processing') AS processing,
                count(*) FILTER(WHERE status='failed') AS failed,
                COALESCE(max(EXTRACT(EPOCH FROM clock_timestamp()-created_at))
                    FILTER(WHERE status='pending'),0)::float8 AS oldest_pending_seconds,
                (SELECT count(*) FROM knowledge_workers
                    WHERE heartbeat_at>now()-interval '30 seconds') AS workers,
                (SELECT count(*) FROM knowledge_file_deletions) AS cleanup_pending
                FROM documents""")
                    )
                )
                .mappings()
                .one()
            )
            return dict(row)
