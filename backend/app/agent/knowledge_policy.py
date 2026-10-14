"""知识问答的执行约束：事实解释先检索，问候和明确的数据查询沿用原工具路由。"""

import re
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage

from app.core.exceptions import BadRequestError
from app.core.logging import get_logger

SMALL_TALK = frozenset({
    "你好", "您好", "嗨", "谢谢", "多谢", "好的", "好", "收到", "再见", "你是谁", "你能做什么",
    "hello", "hi", "thanks", "thank you", "bye", "help",
})
EXPLANATION = re.compile(
    r"是什么|什么是|介绍|解释|说明|规则|机制|教程|指南|如何|怎么|为什么|周期|时间表|限制|文档|知识库"
    r"|\b(?:what\s+(?:is|are)|explain|how|why|tell me about)\b", re.I
)
DATA_QUERY = re.compile(
    r"余额|持仓|资产|我有多少|多少钱|价格|币价|行情|报价|值多少|汇总|合计|支持.*(?:链|币)|哪些链|哪些币"
    r"|\b(?:balance|holdings?|prices?|portfolio|supported chains?|supported tokens?)\b", re.I
)


def needs_retrieval(query: str) -> bool:
    """平台不提供脱离知识库的一般事实回答；明确的数据查询仍交给资产/行情工具。"""
    normalized = query.strip().strip("。！？!?.,， ").lower()
    if not normalized or normalized in SMALL_TALK:
        return False
    return bool(EXPLANATION.search(query)) or not DATA_QUERY.search(query)


class RequireKnowledge(AgentMiddleware):
    @hook_config(can_jump_to=["tools"])
    async def abefore_model(self, state, runtime):
        messages = list(state["messages"])
        last_user = next((i for i in range(len(messages) - 1, -1, -1)
                          if messages[i].type == "human"), None)
        if last_user is None:
            return None
        query = messages[last_user].text
        if not needs_retrieval(query):
            return None
        if len(query) > 1000:
            raise BadRequestError("知识库问题超过1000字，请缩短问题", code="knowledge_query_too_long")
        current = messages[last_user + 1:]
        call_ids = {
            call["id"] for message in current if message.type == "ai"
            for call in message.tool_calls if call["name"] == "search_docs"
        }
        if any(m.type == "tool" and m.tool_call_id in call_ids and m.status == "success" for m in current):
            return None
        get_logger(__name__).info("knowledge.required")
        # 使用标准工具节点，因此鉴权、改写、工具轨迹、计数上限和 LangSmith 追踪仍然生效。
        return {"messages": [AIMessage(content="", id=str(uuid4()),
                                               additional_kwargs={"tool_origin": "policy"}, tool_calls=[{
            "name": "search_docs", "args": {"query": query}, "id": str(uuid4()), "type": "tool_call",
        }])], "jump_to": "tools"}
