"""知识库检索工具：改写问题、检索、重排，再把带编号的原文片段交给模型作答。"""

import json

from langchain.tools import ToolRuntime, tool
from pydantic import Field

from app.agent.context import RunContext
from app.agent.rag import build_context, label_hits, recent_dialogue
from app.agent.tools.base import Arguments
from app.agent.tools.envelope import make_result
from app.core.exceptions import AppError, KnowledgeRetrievalError, UnauthorizedError
from app.core.logging import span

# 检索条数固定，不开放给模型：它对「需要几片」没有可判断的依据，多给一个旋钮只会多一次
# 参数校验往返（越界会变成一条错误消息回给模型，让它重试一次）。
SEARCH_LIMIT = 5


class SearchArguments(Arguments):
    """检索参数：一句自然语言问题。"""

    query: str = Field(
        min_length=1,
        max_length=1000,
        description="要查找的内容，用整句话描述；必须脱离对话也能独立看懂，指代词先换成具体名字",
    )


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
        rewritten = await context.rewrite(query, recent_dialogue((runtime.state or {}).get("messages")))
        try:
            data = await context.client.request(
                "POST", "/knowledge/search", context.token,
                json={"query": rewritten.query, "limit": SEARCH_LIMIT},
            )
        except UnauthorizedError:
            raise
        except AppError as error:
            incompatible = (
                error.code == "embedding_version_mismatch" or getattr(error, "upstream_code", None) == 40904
            )
            failure = KnowledgeRetrievalError(
                "知识库索引不兼容，请由管理员重建索引" if incompatible else "知识库检索暂不可用，请稍后重试",
                code="knowledge_index_incompatible" if incompatible else "knowledge_retrieval_failed",
            )
            failure.upstream_code = getattr(error, "upstream_code", None)
            raise failure from error
        # 编号在拼上下文之前分配，卡片与模型看到的 [1][2] 因此是同一套。
        hits = label_hits(data["hits"])
        make_result(
            context,
            "knowledge",
            {"query": data["query"], "hits": hits, "excluded_documents": data.get("excluded_documents", 0)},
            source_desc=f"知识库检索 · {len(hits)} 个片段",
        )
        text = build_context(hits)
        if not text:
            return json.dumps(
                {
                    "query": data["query"],
                    "hits": [],
                    "status": "no_answer",
                    "note": "知识库没有找到足够相关的依据。请明确说明未找到依据，不要猜测答案或编造引用。",
                },
                ensure_ascii=False,
            )
        if data.get("excluded_documents", 0):
            text += "\n部分文档的索引版本不兼容，未参与本次检索；以上不是完整知识库的检索范围。"
        # 原文块原样交给模型：要它引用出处，就不能让片段先过一遍摘要。
        return text
