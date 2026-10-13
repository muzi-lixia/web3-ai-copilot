"""把带编号的片段拼成引用块，作为模型作答的依据。

超出预算的片段整块丢掉，不做半截截断：半截原文比没有原文更容易被当成完整依据。
"""

# 一次检索交给模型的片段总长度上限（字符）：按每片 800 字、留出若干片的余量。
BUDGET_CHARS = 6000


def _locator(hit) -> str:
    """拼出处：标题路径、页码、相似度；缺哪段就不写哪段。"""
    parts = []
    if hit.get("heading"):
        parts.append(hit["heading"])
    start, end = hit.get("page_start"), hit.get("page_end")
    if start and end:
        parts.append(f"第 {start} 页" if start == end else f"第 {start}-{end} 页")
    parts.append(f"相似度 {float(hit['score']):.4f}")
    return " · ".join(parts)


def build(hits, budget=BUDGET_CHARS) -> str:
    """按编号拼成可引用的原文块；没有片段、或第一片就装不下时返回空串。"""
    blocks = []
    used = 0
    for hit in hits:
        block = f"[{hit['index']}] 《{hit['title']}》 · {_locator(hit)}\n{str(hit['content']).strip()}"
        if used + len(block) > budget:
            break
        blocks.append(block)
        used += len(block) + 1
    return "\n\n".join(blocks)
