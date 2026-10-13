"""片段重排：在向量相似度之外补一层词面覆盖。

纯确定性代码，不调模型：重排要能在一轮里随时重算，不该再引入一次网络往返。
"""

# 向量分与词面覆盖的权重：语义为主，词面只在语义接近时微调先后。
SEMANTIC_WEIGHT = 0.6

# 二元组在中文里近似「词」：没有分词器时，字符覆盖比整句匹配稳。
GRAM = 2


def _grams(text: str) -> set[str]:
    """取字符二元组；只保留字母、数字与汉字，标点与空格不参与匹配。"""
    clean = "".join(ch for ch in text.lower() if ch.isalnum())
    return {clean[i : i + GRAM] for i in range(len(clean) - GRAM + 1)}


def _coverage(query: str, content: str) -> float:
    """问题里的二元组有多大比例出现在片段里。"""
    wanted = _grams(query)
    if not wanted:
        return 0.0
    return len(wanted & _grams(content)) / len(wanted)


def rerank(hits, query):
    """按「归一化相似度 × 0.6 + 词面覆盖 × 0.4」重排，返回新列表。

    相似度先归一化再融合：两者量纲不同，直接相加会让长片段凭覆盖面大而占便宜。
    只改顺序，不改动片段本身，也不往片段里加字段。
    """
    if len(hits) < 2:
        return list(hits)
    scores = [float(hit["score"]) for hit in hits]
    low, high = min(scores), max(scores)
    span = high - low

    def blended(hit):
        semantic = 1.0 if span == 0 else (float(hit["score"]) - low) / span
        return SEMANTIC_WEIGHT * semantic + (1 - SEMANTIC_WEIGHT) * _coverage(query, hit["content"])

    return sorted(hits, key=blended, reverse=True)
