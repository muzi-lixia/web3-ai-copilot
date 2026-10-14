"""知识库离线链路：加载 → 清洗 → 切分 → 向量化，产出可直接入库的分片。"""

import asyncio

from app.core.logging import span
from app.infrastructure.database.vector import to_literal
from app.infrastructure.providers.embedding import EmbeddingError, embed
from app.modules.knowledge.version import embedding_version
from app.workers.knowledge import cleaner, loader, splitter
from app.workers.knowledge.errors import FailureKind, IngestionError
from app.workers.knowledge.loader import LoadedBlock
from app.workers.knowledge.splitter import Chunk


class KnowledgePipeline:
    """把一份原件处理成待写入的分片。读原件与写库由调用方负责。"""

    def __init__(self, settings):
        """持有配置，用于取向量与校验维度。"""
        self.settings = settings

    async def process(self, suffix: str, content: bytes) -> list[dict]:
        """顺序跑完四步，返回分片字典列表。任何一步失败都带阶段与分类抛出，由调用方记原因。"""
        chunks = await asyncio.to_thread(self._prepare, suffix, content)
        # 向量带上标题路径：标题写的就是本节主题，正文常常不复述它（表格片尤其明显），
        # 只吃 content 会让「拿标题当问题」的问法查不到这一节。
        texts = [f"{chunk.heading}\n{chunk.content}" if chunk.heading else chunk.content for chunk in chunks]
        with span("knowledge.stage", stage="embed"):
            vectors = await self._embed(texts)
        return [
            {
                "ordinal": chunk.ordinal,
                "heading": chunk.heading,
                "content": chunk.content,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "kind": chunk.kind,
                "embedding": to_literal(vector),
                "embedding_version": embedding_version(self.settings),
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]

    def _prepare(self, suffix: str, content: bytes) -> list[Chunk]:
        """同步解析离开事件循环，避免阻塞任务续租；线程不执行任何数据库写入。"""
        with span("knowledge.stage", stage="load"):
            blocks = self._load(suffix, content)
        with span("knowledge.stage", stage="clean"):
            cleaned = self._clean(blocks)
        with span("knowledge.stage", stage="split"):
            return self._split(suffix, cleaned)

    @staticmethod
    def _load(suffix: str, content: bytes) -> list[LoadedBlock]:
        """解析原件；解析器抛错或一个块都提不出来，都算文件侧的问题。"""
        try:
            blocks = loader.load(suffix, content)
        except Exception as error:
            raise IngestionError(
                stage="load",
                kind=FailureKind.INPUT,
                code="load.parse_failed",
                message="原件解析失败，请检查文件格式",
            ) from error
        if not blocks:
            raise IngestionError(
                stage="load",
                kind=FailureKind.INPUT,
                code="load.empty_text",
                message="未能从文件中提取到任何文本",
            )
        return blocks

    @staticmethod
    def _clean(blocks: list[LoadedBlock]) -> list[LoadedBlock]:
        """按块类型与来源分流清洗；这一层出错只可能是代码缺陷，算服务侧的问题。"""
        try:
            return cleaner.clean(blocks)
        except Exception as error:
            raise IngestionError(
                stage="clean",
                kind=FailureKind.SYSTEM,
                code="clean.failed",
                message="文档清洗失败，请联系管理员",
            ) from error

    @staticmethod
    def _split(suffix: str, blocks: list[LoadedBlock]) -> list[Chunk]:
        """切分；切完一片不剩说明内容形态超出切分器的处理范围，算文件侧的问题。"""
        try:
            chunks = splitter.split(suffix, blocks)
        except Exception as error:
            raise IngestionError(
                stage="split",
                kind=FailureKind.SYSTEM,
                code="split.failed",
                message="文档切分失败，请联系管理员",
            ) from error
        if not chunks:
            raise IngestionError(
                stage="split",
                kind=FailureKind.INPUT,
                code="split.empty_chunk",
                message="切分后没有得到任何分片",
            )
        return chunks

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        """取向量；连不上或维度不符都要靠环境恢复，算服务侧的问题。"""
        try:
            return await embed(
                texts,
                self.settings,
                timeout=self.settings.embedding_ingest_timeout_seconds,
                batch_size=self.settings.embedding_ingest_batch_size,
                base_url=self.settings.embedding_ingest_base_url or None,
            )
        except EmbeddingError as error:
            raise IngestionError(
                stage="embed",
                kind=FailureKind.SYSTEM,
                code="embed.failed",
                message="向量化失败，请检查模型服务及向量配置",
            ) from error
