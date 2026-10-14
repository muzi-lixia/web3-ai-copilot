"""知识库 HTTP 业务：上传登记、原件管理与在线检索，不执行文档解析任务。"""

import asyncio
import hashlib
import os
from pathlib import Path
from uuid import uuid4

from app.core.exceptions import BadRequestError, ConflictError, NotFoundError, UpstreamError
from app.core.logging import get_logger
from app.infrastructure.database.vector import to_literal
from app.infrastructure.providers.embedding import EmbeddingError, embed
from app.modules.knowledge.ranking import rerank
from app.modules.knowledge.repository import KnowledgeRepository
from app.modules.knowledge.storage import document_path
from app.modules.knowledge.version import embedding_version

# 后缀决定离线链路上用哪个加载器；不在这张表里的类型一律拒收。
MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class KnowledgeService:
    """文档登记、原件管理与在线检索。调用方保证 content 已按配置上限限过体积。"""

    def __init__(self, database, settings):
        """持有数据库连接层与存储配置，不在内存里保留上传内容。"""
        self.settings = settings
        self.repository = KnowledgeRepository(database)
        self.embedding_slots = asyncio.Semaphore(settings.embedding_query_concurrency)

    async def upload(self, filename: str, content: bytes) -> dict:
        """校验类型与内容后落盘并登记，返回新文档的元数据。

        内容摘要相同的文件直接拒绝：同一份文件入库两次会得到两份等价向量，
        检索时互相挤占 top-k 名额。
        """
        suffix = Path(filename).suffix.lower()
        if suffix not in MIME_BY_SUFFIX:
            raise BadRequestError(f"暂不支持 {suffix or '无扩展名'} 类型的文件")
        if not content:
            raise BadRequestError("文件内容为空")

        digest = await asyncio.to_thread(lambda: hashlib.sha256(content).hexdigest())
        if await self.repository.find_by_hash(digest):
            raise ConflictError("该文件已在知识库中，请勿重复上传", code="document_exists")

        document_id = uuid4().hex
        # 先持久化清理意图，再原子落盘；成功登记文档时在同一事务取消清理。
        await self.repository.schedule_cleanup(document_id, suffix, delay=3600)
        await asyncio.to_thread(self._write, document_id, suffix, content)
        try:
            created = await self.repository.create(
                {
                    "id": document_id,
                    "title": Path(filename).stem or filename,
                    "filename": filename,
                    "mime": MIME_BY_SUFFIX[suffix],
                    "size": len(content),
                    "sha256": digest,
                }
            )
        except ConflictError:
            await self.repository.schedule_cleanup(document_id, suffix)
            raise
        if not created:
            await self.repository.schedule_cleanup(document_id, suffix)
            raise ConflictError("该文件已在知识库中，请勿重复上传", code="document_exists")
        return await self.repository.read(document_id)

    async def listing(self) -> list[dict]:
        """列出全部文档元数据，供管理界面展示处理状态。"""
        version = embedding_version(self.settings)
        return [
            {**doc, "index_compatible": doc["status"] == "ready" and doc.get("embedding_version") == version}
            for doc in await self.repository.listing()
        ]

    async def read(self, document_id: str) -> dict:
        """读单条文档元数据；不存在按未找到处理。"""
        document = await self.repository.read(document_id)
        if document is None:
            raise NotFoundError("文档不存在")
        return {
            **document,
            "index_compatible": document["status"] == "ready"
            and document.get("embedding_version") == embedding_version(self.settings),
        }

    async def remove(self, document_id: str) -> None:
        """删除文档元数据与原文件；分片随外键级联删除。"""
        document = await self.read(document_id)
        if not await self.repository.delete(document_id):
            raise NotFoundError("文档不存在")
        path = self._path(document_id, Path(document["filename"]).suffix.lower())
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
            await asyncio.to_thread(path.with_suffix(path.suffix + ".upload").unlink, missing_ok=True)
            await self.repository.finish_cleanup(document_id)
        except OSError:
            get_logger(__name__).warning("knowledge.cleanup_deferred", extra={"document_id": document_id})

    async def search(self, query: str, limit: int) -> dict:
        """把问题转成向量后检索分片，返回命中片段与出处。

        检索与入库用的是同一个向量化服务：换了模型而没重建 chunks.embedding，
        取到的相似度就没有意义，所以维度校验仍然照跑。
        """
        version = embedding_version(self.settings)
        index = await self.repository.index_status(version)
        if index["ready"] and not index["compatible"]:
            raise ConflictError("知识库向量版本不兼容，请由管理员重建索引", code="embedding_version_mismatch")
        candidates = []
        if index["compatible"]:
            try:
                async with asyncio.timeout(self.settings.embedding_query_timeout_seconds):
                    async with self.embedding_slots:
                        vector = (
                            await embed(
                                [query],
                                self.settings,
                                timeout=self.settings.embedding_query_timeout_seconds,
                                batch_size=1,
                                base_url=self.settings.embedding_query_base_url or None,
                            )
                        )[0]
            except TimeoutError:
                raise UpstreamError("在线检索向量化等待超时，请稍后重试") from None
            except EmbeddingError:
                raise UpstreamError("在线检索向量化服务暂不可用") from None
            candidates = await self.repository.search(
                to_literal(vector), min(100, max(20, limit * 4)), version
            )
        # 使用原始余弦分判门槛，避免重排的相对归一化把无关的最高分片抬成相关。
        accepted = [hit for hit in candidates if hit["score"] >= self.settings.knowledge_search_min_score]
        hits = rerank(accepted, query)[:limit]
        return {
            "query": query,
            "hits": hits,
            "status": "matched" if hits else "no_answer",
            "reason": None if hits else ("below_threshold" if index["compatible"] else "empty_index"),
            "embedding_version": version,
            "excluded_documents": index["ready"] - index["compatible"],
        }

    async def reindex(self, document_id: str) -> dict:
        """管理员从原件重建向量，不能直接给旧向量补版本标签。"""
        await self.read(document_id)
        if not await self.repository.reindex(document_id):
            raise ConflictError("仅就绪或失败的文档可重新索引", code="document_not_reindexable")
        return await self.read(document_id)

    async def retry(self, document_id: str) -> dict:
        """把处理失败的文档重新排进队列，返回刷新后的元数据。

        只有失败态改得动，判断落在更新语句里，两次请求同时点重试也不会重复排队。
        """
        await self.read(document_id)
        if not await self.repository.retry(document_id):
            raise ConflictError("只有处理失败的文档可以重试", code="document_not_failed")
        return await self.read(document_id)

    def _path(self, document_id: str, suffix: str) -> Path:
        """原件按文档 id 加原扩展名保存，避免同名原始文件互相覆盖。"""
        return document_path(self.settings, document_id, suffix)

    def _write(self, document_id: str, suffix: str, content: bytes) -> None:
        """按 id 落盘；目录不存在就创建，写入失败直接冒泡让整个上传失败。"""
        path = self._path(document_id, suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".upload")
        temporary.write_bytes(content)
        os.replace(temporary, path)
