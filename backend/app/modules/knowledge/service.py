"""知识库：上传登记、原件管理与离线链路调度。"""

import hashlib
from pathlib import Path
from uuid import uuid4

from app.core.exceptions import BadRequestError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.modules.knowledge.embedder import embed, to_literal
from app.modules.knowledge.errors import FailureKind, IngestionError
from app.modules.knowledge.pipeline import KnowledgePipeline
from app.modules.knowledge.repository import KnowledgeRepository

logger = get_logger(__name__)

# 后缀决定离线链路上用哪个加载器；不在这张表里的类型一律拒收。
MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class KnowledgeService:
    """文档登记、原件管理与离线链路调度。调用方保证 content 已按配置上限限过体积。"""

    def __init__(self, database, settings):
        """持有数据库连接层与存储配置，不在内存里保留上传内容。"""
        self.settings = settings
        self.repository = KnowledgeRepository(database)
        self.pipeline = KnowledgePipeline(settings)

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

        digest = hashlib.sha256(content).hexdigest()
        if await self.repository.find_by_hash(digest):
            raise ConflictError("该文件已在知识库中，请勿重复上传", code="document_exists")

        document_id = uuid4().hex
        # 先落盘再写库：有行无文件会破坏后续解析，多一个孤儿文件则无害。
        self._write(document_id, suffix, content)
        await self.repository.create(
            {
                "id": document_id,
                "title": Path(filename).stem or filename,
                "filename": filename,
                "mime": MIME_BY_SUFFIX[suffix],
                "size": len(content),
                "sha256": digest,
            }
        )
        return await self.repository.read(document_id)

    async def listing(self) -> list[dict]:
        """列出全部文档元数据，供管理界面展示处理状态。"""
        return await self.repository.listing()

    async def read(self, document_id: str) -> dict:
        """读单条文档元数据；不存在按未找到处理。"""
        document = await self.repository.read(document_id)
        if document is None:
            raise NotFoundError("文档不存在")
        return document

    async def remove(self, document_id: str) -> None:
        """删除文档元数据与原文件；分片随外键级联删除。"""
        document = await self.read(document_id)
        if not await self.repository.delete(document_id):
            raise NotFoundError("文档不存在")
        self._path(document_id, Path(document["filename"]).suffix.lower()).unlink(missing_ok=True)

    async def run_pending(self) -> bool:
        """处理一条待处理文档，返回本轮是否还有事可做。

        轮询循环靠返回值决定接着取还是歇一下。单篇失败只写进该文档的原因并记一条日志，
        不向外抛：一篇坏文件不该让整条队列停住。
        """
        document = await self.repository.claim()
        if document is None:
            return False
        suffix = Path(document["filename"]).suffix.lower()
        try:
            content = self._path(document["id"], suffix).read_bytes()
        except OSError as error:
            await self._failed(
                document["id"],
                IngestionError(
                    stage="load",
                    kind=FailureKind.INPUT,
                    code="load.read_failed",
                    message=f"读取原件失败：{error}",
                ),
            )
            return True
        try:
            chunks = await self.pipeline.process(suffix, content)
        except IngestionError as error:
            await self._failed(document["id"], error)
            return True
        except Exception as error:
            # 没被包装过的异常说明是未预料的缺陷，按服务侧记，同样不中断队列。
            await self._failed(
                document["id"],
                IngestionError(
                    stage="unknown",
                    kind=FailureKind.SYSTEM,
                    code="unexpected",
                    message=f"{type(error).__name__}: {error}",
                ),
            )
            return True
        await self.repository.complete(document["id"], chunks)
        return True

    async def recover(self) -> int:
        """把上次进程中断留下的「处理中」退回待处理，返回退回条数。"""
        return await self.repository.retry_stuck()

    async def search(self, query: str, limit: int) -> dict:
        """把问题转成向量后检索分片，返回命中片段与出处。

        检索与入库用的是同一个向量化服务：换了模型而没重建 chunks.embedding，
        取到的相似度就没有意义，所以维度校验仍然照跑。
        """
        vector = (await embed([query], self.settings))[0]
        return {"query": query, "hits": await self.repository.search(to_literal(vector), limit)}

    async def retry(self, document_id: str) -> dict:
        """把处理失败的文档重新排进队列，返回刷新后的元数据。

        只有失败态改得动，判断落在更新语句里，两次请求同时点重试也不会重复排队。
        """
        await self.read(document_id)
        if not await self.repository.retry(document_id):
            raise ConflictError("只有处理失败的文档可以重试", code="document_not_failed")
        return await self.read(document_id)

    async def _failed(self, document_id: str, failure: IngestionError) -> None:
        """写失败记录并记一条日志；日志只带受控枚举，文件名与正文都不进去。"""
        await self.repository.fail(
            document_id, str(failure), stage=failure.stage, error_code=failure.code
        )
        logger.warning(
            "knowledge.failed",
            extra={
                "document_id": document_id,
                "stage": failure.stage,
                "error_code": failure.code,
                "kind": failure.kind,
            },
        )

    def _path(self, document_id: str, suffix: str) -> Path:
        """原件按文档 id 加原扩展名保存，避免同名原始文件互相覆盖。"""
        return Path(self.settings.knowledge_storage_dir) / f"{document_id}{suffix}"

    def _write(self, document_id: str, suffix: str, content: bytes) -> None:
        """按 id 落盘；目录不存在就创建，写入失败直接冒泡让整个上传失败。"""
        path = self._path(document_id, suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
