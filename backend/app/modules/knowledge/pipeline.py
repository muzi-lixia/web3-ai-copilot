"""知识库离线链路：加载 → 清洗 → 切分 → 向量化，产出可直接入库的分片。"""

from app.modules.knowledge import cleaner, loader, splitter
from app.modules.knowledge.embedder import EmbeddingError, embed, to_literal
from app.modules.knowledge.errors import FailureKind, IngestionError
from app.modules.knowledge.loader import LoadedBlock
from app.modules.knowledge.splitter import Chunk


class KnowledgePipeline:
    """把一份原件处理成待写入的分片。读原件与写库由调用方负责。"""

    def __init__(self, settings):
        """持有配置，用于取向量与校验维度。"""
        self.settings = settings

    async def process(self, suffix: str, content: bytes) -> list[dict]:
        """顺序跑完四步，返回分片字典列表。任何一步失败都带阶段与分类抛出，由调用方记原因。"""
        blocks = self._load(suffix, content)
        chunks = self._split(suffix, self._clean(blocks))
        # 向量带上标题路径：标题写的就是本节主题，正文常常不复述它（表格片尤其明显），
        # 只吃 content 会让「拿标题当问题」的问法查不到这一节。
        texts = [
            f"{chunk.heading}\n{chunk.content}" if chunk.heading else chunk.content for chunk in chunks
        ]
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
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]

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
                message=f"解析文件失败：{error}",
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
                message=f"清洗内容失败：{error}",
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
                message=f"切分内容失败：{error}",
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
            return await embed(texts, self.settings)
        except EmbeddingError as error:
            raise IngestionError(
                stage="embed",
                kind=FailureKind.SYSTEM,
                code="embed.failed",
                message=f"向量化失败：{error}",
            ) from error
