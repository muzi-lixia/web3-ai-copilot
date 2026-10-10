"""框架管理消息状态；预算只裁剪完整旧轮次，不再调度后台模型摘要。"""

import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import RemoveMessage

from app.core.exceptions import ContextTooLargeError
from app.core.logging import span


class ContextBudget(AgentMiddleware):
    def __init__(self, settings):
        self.settings = settings

    async def abefore_model(self, state, runtime):
        """UTF-8 字节作保守上界；计入工具 schema、系统提示、输出预留。

        删除旧 user 之前的完整轮次，不拆开 assistant tool_call / ToolMessage 对。
        单轮超预算明确拒绝，不能靠模型服务静默截断。
        """
        from app.agent.tools import TOOLS

        s = self.settings
        window = s.model_context_window or 16384
        budget = min(s.agent_context_tokens, window - s.model_output_tokens - s.model_context_reserve)
        overhead = len(json.dumps([t.tool_call_schema.model_json_schema() for t in TOOLS]).encode()) + 2048
        messages = list(state["messages"])
        removed = []

        def size():
            return overhead + sum(len(m.model_dump_json().encode()) for m in messages)

        while size() > budget:
            next_user = next((i for i, m in enumerate(messages[1:], 1) if m.type == "human"), None)
            if next_user is None:
                raise ContextTooLargeError("本轮输入超过模型窗口，请缩短问题")
            removed.extend(messages[:next_user])
            messages = messages[next_user:]
        return {"messages": [RemoveMessage(id=m.id) for m in removed]} if removed else None

    async def awrap_model_call(self, request, handler):
        with span("model.completed", model=self.settings.model_name, provider=self.settings.model_provider):
            return await handler(request)

    async def awrap_tool_call(self, request, handler):
        """业务异常转为安全工具反馈；身份错误不能伪装成普通工具参数错误。"""
        from langchain_core.messages import ToolMessage

        from app.core.exceptions import AppError, UnauthorizedError

        if request.tool is None:
            return await handler(request)
        allowed = set(request.tool.args_schema.model_fields)
        if set(request.tool_call["args"]) - allowed:
            return ToolMessage(
                content="工具参数含未声明字段，请仅使用工具 schema 中的业务参数。",
                tool_call_id=request.tool_call["id"],
                status="error",
            )

        try:
            return await handler(request)
        except UnauthorizedError:
            raise
        except AppError as exc:
            return ToolMessage(
                content=json.dumps(
                    {
                        "status": "failed",
                        "error_code": exc.code,
                        "message": "查询失败。请确认参数或稍后重试；不得编造结果。",
                    }
                ),
                tool_call_id=request.tool_call["id"],
                status="error",
            )
