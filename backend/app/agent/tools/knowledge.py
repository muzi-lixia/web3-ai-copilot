"""知识库检索工具：改写问题、检索、重排，再把带编号的原文片段交给模型作答。"""

import json

from langchain.tools import ToolRuntime, tool
from pydantic import Field

from app.agent.context import RunContext
from app.agent.rag import build_context, label_hits, recent_dialogue, rerank
from app.agent.tools.base import Arguments
from app.agent.tools.envelope import make_result
from app.core.logging import span

# 检索条数固定，不开放给模型：它对「需要几片」没有可判断的依据，多给一个旋钮只会多一次
# 参数校验往返（越界会变成一条错误消息回给模型，让它重试一次）。
SEARCH_LIMIT = 5


class SearchArguments(Arguments):
    """检索参数：一句自然语言问题。"""

    query: str = Field(min_length=1, max_length=1000, description="要查找的内容，用整句话描述")


@tool(
    args_schema=SearchArguments,
    description="""检索平台知识库文档片段，用于回答需要文档原文依据的问题（协议说明、操作指南、平台笔记）。
                仅返回命中的片段，不返回整篇文档。
                禁止场景：用户询问个人资产、代币行情相关问题，不调用此工具。
                每次请求实时检索，无缓存，不得复用对话历史内的文档片段。""",
)
async def search_docs(query: str, runtime: ToolRuntime[RunContext]):
    """检索知识库原文片段。

    Args:
        query: 用自然语言描述要找什么。
        runtime: 服务端可信上下文。
    Returns:
        带编号 [1][2] 的片段原文与出处；没有命中时返回空结果说明。
    """
    with span("tool.completed", tool="search_docs"):
        context = runtime.context
        # 先改写：多轮追问里的「它」「这个」要靠最近几轮对话才能补全指代。
        asked = await context.rewrite(query, recent_dialogue((runtime.state or {}).get("messages")))
        data = await context.client.request(
            "POST",
            "/knowledge/search",
            context.token,
            json={"query": asked, "limit": SEARCH_LIMIT},
        )
        # 编号在拼上下文之前分配，卡片与模型看到的 [1][2] 因此是同一套。
        hits = label_hits(rerank(data["hits"], asked))
        make_result(
            context,
            "knowledge",
            {"query": data["query"], "hits": hits},
            source_desc=f"知识库检索 · {len(hits)} 个片段",
        )
        text = build_context(hits)
        if not text:
            return json.dumps(
                {"query": data["query"], "hits": [], "note": "知识库中没有命中任何片段"},
                ensure_ascii=False,
            )
        # 原文块原样交给模型：要它引用出处，就不能让片段先过一遍摘要。
        return text
