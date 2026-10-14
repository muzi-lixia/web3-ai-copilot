"""调用向量化服务生成检索向量。默认指向本机 Ollama。"""

import httpx

# 单次提交的文本条数：批量太大会让本地模型长时间无响应，太小则往返次数过多。
BATCH_SIZE = 16

# 本机模型在首次加载时较慢，超时给足。
TIMEOUT_SECONDS = 180.0


class EmbeddingError(RuntimeError):
    """向量化失败。消息会写进文档的失败原因，面向运维而不是终端用户。"""


async def embed(
    texts: list[str], settings, *, timeout=TIMEOUT_SECONDS, batch_size=BATCH_SIZE, base_url=None
) -> list[list[float]]:
    """分批取向量，返回顺序与输入一致。"""
    vectors: list[list[float]] = []
    # 本机地址直连：不走环境里的 HTTP 代理，避免被转发到别处而留下一串连接错误。
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        for start in range(0, len(texts), batch_size):
            vectors.extend(await _one_batch(client, texts[start : start + batch_size], settings, base_url))
    return vectors


async def _one_batch(
    client: httpx.AsyncClient, batch: list[str], settings, base_url=None
) -> list[list[float]]:
    """提交一批文本并校验返回条数与维度。"""
    url = f"{(base_url or settings.embedding_base_url).rstrip('/')}/api/embed"
    try:
        response = await client.post(url, json={"model": settings.embedding_model, "input": batch})
    except httpx.HTTPError as error:
        raise EmbeddingError("无法连接向量化服务") from error
    if response.status_code != 200:
        raise EmbeddingError(f"向量化服务返回 HTTP {response.status_code}")

    vectors = response.json().get("embeddings") or []
    if len(vectors) != len(batch):
        raise EmbeddingError(f"向量条数不匹配：提交 {len(batch)} 条，返回 {len(vectors)} 条")
    for vector in vectors:
        if len(vector) != settings.knowledge_embedding_dimensions:
            raise EmbeddingError(
                f"向量维度 {len(vector)} 与配置的 {settings.knowledge_embedding_dimensions} 不一致，"
                f"换过模型就要同步重建 chunks.embedding 列"
            )
    return vectors
