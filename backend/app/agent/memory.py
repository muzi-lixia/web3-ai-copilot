"""框架管理消息状态；预算只裁剪完整旧轮次，不再调度后台模型摘要。"""

import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import RemoveMessage

from app.core.exceptions import ContextTooLargeError
from app.core.logging import span


class ContextBudget(AgentMiddleware):
    """把对话压进模型窗口，并在工具执行前收紧参数边界。

    预算用保守计数而不是真实 tokenizer：宁可少算，也不能指望模型服务静默截断。
    """

    def __init__(self, settings):
        """固定本轮设置快照；预算在每次模型调用前按当前消息重新计算。"""
        self.settings = settings

    async def abefore_model(self, state, runtime):
        """UTF-8 字节作保守上界；计入工具 schema、系统提示、输出预留。

        删除旧 user 之前的完整轮次，不拆开 assistant tool_call / ToolMessage 对。
        单轮超预算明确拒绝，不能靠模型服务静默截断。
        """
        from app.agent.tools import TOOLS

        s = self.settings
        # 预算 = 配置上限 与 (窗口 - 输出预留 - 安全余量) 取小值。
        window = s.model_context_window or 16384
        budget = min(s.agent_context_tokens, window - s.model_output_tokens - s.model_context_reserve)
        # overhead 是工具 schema 的固定占用，必须计入体积，否则会低估输入。
        overhead = len(json.dumps([t.tool_call_schema.model_json_schema() for t in TOOLS]).encode()) + 2048
        messages = list(state["messages"])
        removed = []

        def size():
            """估算当前消息集的字节体积（含工具 schema 的固定开销）。

            按 UTF-8 字节估算：中文比字符数占得更多；overhead 已包含工具 schema。
            """
            return overhead + sum(len(m.model_dump_json().encode()) for m in messages)

        # 超预算就从最旧的整轮开始丢：切点只放在 human 消息上，保证不会把 assistant 的
        # tool_call 与它的 ToolMessage 结果拆开（拆开会让历史语义残缺）。从索引 1 开始找，
        # 是为了永远保留当前这一轮的起点。
        while size() > budget:
            next_user = next((i for i, m in enumerate(messages[1:], 1) if m.type == "human"), None)
            if next_user is None:
                # 只剩最后一轮还超预算：明确拒绝，不能指望模型服务静默截断。
                raise ContextTooLargeError("本轮输入超过模型窗口，请缩短问题")
            removed.extend(messages[:next_user])
            messages = messages[next_user:]
        return {"messages": [RemoveMessage(id=m.id) for m in removed]} if removed else None

    async def awrap_model_call(self, request, handler):
        """只记录模型调用埋点，不干预请求内容、重试与错误传播。"""
        with span("model.completed", model=self.settings.model_name, provider=self.settings.model_provider):
            return await handler(request)

    async def awrap_tool_call(self, request, handler):
        """业务异常转为安全工具反馈；身份错误不能伪装成普通工具参数错误。"""
        from langchain_core.messages import ToolMessage

        from app.core.exceptions import AppError, UnauthorizedError

        if request.tool is None:
            return await handler(request)
        # 只放行 schema 里声明的业务参数：模型可能借框架字段（如 runtime）注入身份。
        # 这里回一条工具错误让模型自我纠正，而不是抛异常打断整轮。
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
            # 身份失效必须向上抛：伪装成普通工具失败会让对话继续跑在一个无效会话上。
            raise
        except AppError as exc:
            # 业务错误转成安全反馈：既不泄露上游细节，也明确要求模型不得编造结果。
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
