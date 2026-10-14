"""Agent 中间件：历史实时数据过滤、上下文预算、工具调用治理。"""

import json
import time

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, RemoveMessage

from app.core.exceptions import ContextTooLargeError
from app.core.logging import span


def _estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数，不依赖任何 tokenizer。

    规则：ASCII 四字符算 1 token，非 ASCII 一字算 1 token。两个系数都偏高
    （英文真实约 4 字符 / token，中文约 1.5 字 / token），偏差方向固定为高估 ——
    宁可多算，也不能让上游按真实窗口把历史静默截断。
    """
    wide = sum(1 for ch in text if ord(ch) > 127)
    return (len(text) - wide) // 4 + wide + 1


# 结果随外部状态变化的工具：历史往返只保留「查过什么」，数值一律不留。
# 链目录这类静态配置不在其中 —— 重查也是同一份数据，作废只会白多一次往返。
LIVE_TOOLS = frozenset(
    {
        "get_token_price",
        "query_assets",
        "aggregate_results",
    }
)


def _args_brief(args: dict) -> str:
    """把调用参数压成一行，用于标识「查的是哪个币」；长值截断，避免摘要本身吃预算。"""
    parts = []
    for key, value in (args or {}).items():
        if value is None:
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        parts.append(f"{key}={text[:24]}")
    return ", ".join(parts)


class LiveDataFilter(AgentMiddleware):
    """喂给模型前，把历史轮次里的实时工具往返换成不含数值的摘要。

    只改本轮发送的消息，不动持久化状态；当前轮的完整往返原样保留，否则模型拿到
    工具结果后无从总结。历史里「上一轮查的是哪个币」这层指代由摘要留住，旧报价
    则不再进入模型上下文。
    """

    async def awrap_model_call(self, request, handler):
        """每轮每次模型调用都重新切分，工具往返不会改变当前轮起点。"""
        return await handler(request.override(messages=self._filtered(request.messages)))

    def _filtered(self, messages):
        """按轮替换：历史轮次里出现过实时工具的换成摘要，其余原样保留。

        最后一组即当前轮，无论它调了什么工具都整组跳过 —— 工具刚返回时模型正要
        据此总结，把它一起换掉等于让本轮查了个空。
        """
        turns = self._turns(messages)
        if not turns:
            return []
        out = []
        for turn in turns[:-1]:
            calls = self._live_calls(turn)
            if calls:
                # 用户原话保留 —— 其中的数字是用户自己给的比较阈值，不是工具报价。
                out.extend((turn[0], AIMessage(content=self._note(calls))))
            else:
                out.extend(turn)
        out.extend(turns[-1])
        return out

    @staticmethod
    def _turns(messages):
        """以 human 消息为界切轮；开头若不是 human 也自成一段，避免整段被吞。"""
        turns = []
        for message in messages:
            if not turns or message.type == "human":
                turns.append([message])
            else:
                turns[-1].append(message)
        return turns

    @staticmethod
    def _live_calls(turn):
        """找出该轮命中的实时工具调用；没有就返回空列表。"""
        for message in turn:
            if message.type == "ai" and message.tool_calls:
                live = [call for call in message.tool_calls if call["name"] in LIVE_TOOLS]
                if live:
                    return live
        return []

    @staticmethod
    def _note(calls):
        """摘要只用工具名与调用参数构建，不引用任何工具返回值。"""
        named = "、".join(f"{call['name']}({_args_brief(call['args'])})" for call in calls)
        return (
            f"（上一轮调用过实时数据工具：{named}。"
            "返回值已失效，不得当作当前数据引用，需要时重新调用。）"
        )


class ContextBudget(AgentMiddleware):
    """把对话压进模型窗口，并在工具执行前收紧参数边界。

    预算用估算而不是真实 tokenizer：少一个带词表的依赖，偏差方向固定为高估。
    """

    def __init__(self, settings):
        """固定本轮设置快照；预算在每次模型调用前按当前消息重新计算。"""
        self.settings = settings

    async def abefore_model(self, state, runtime):
        """按估算 token 作保守上界；计入工具 schema 与全部历史消息。

        系统提示不在 state 的 messages 里，这里计不到 —— 它是固定开销，在 16384
        的预算下占比很小，但实际余量会比算出来的略少一点。

        删除旧 user 之前的完整轮次，不拆开 assistant tool_call / ToolMessage 对。
        单轮超预算明确拒绝，不能靠模型服务静默截断。
        """
        from app.agent.tools import TOOLS

        s = self.settings
        # 预算 = 配置上限 与 (窗口 - 输出预留 - 安全余量) 取小值。
        window = s.model_context_window or 16384
        budget = min(s.agent_context_tokens, window - s.model_output_tokens - s.model_context_reserve)
        # overhead 是工具 schema 的固定占用，必须计入体积，否则会低估输入。
        # ensure_ascii=False：schema 里的中文 description 按原文计，转成 \uXXXX 会重复高估。
        # 这里不再额外加常数余量 —— 余量已由 model_context_reserve 统一留出，重复预留会让
        # 预算被两个说不清来路的数字同时侵蚀。
        overhead = _estimate_tokens(
            json.dumps([t.tool_call_schema.model_json_schema() for t in TOOLS], ensure_ascii=False)
        )
        messages = list(state["messages"])
        removed = []

        def size():
            """估算当前消息集的 token 数（含工具 schema 的固定开销）。

            overhead 已含工具 schema；消息体用 _estimate_tokens 逐条累加。
            """
            return overhead + sum(_estimate_tokens(m.model_dump_json()) for m in messages)

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
        context = request.runtime.context
        # 工具调用轨迹：所有工具调用都经过这里，所以只在这一处记录就不会漏。
        # 记在参数校验之前 —— 参数不合规被拒的调用同样要留痕，那正是要观测的部分。
        call = {"tool": request.tool_call["name"], "args": request.tool_call["args"]}
        context.tool_calls.append(call)
        # 只放行 schema 里声明的业务参数：模型可能借框架字段（如 runtime）注入身份。
        # 这里回一条工具错误让模型自我纠正，而不是抛异常打断整轮。
        allowed = set(request.tool.args_schema.model_fields)
        if set(request.tool_call["args"]) - allowed:
            call["status"] = "rejected"
            return ToolMessage(
                content="工具参数含未声明字段，请仅使用工具 schema 中的业务参数。",
                tool_call_id=request.tool_call["id"],
                status="error",
            )

        # 耗时只包住 handler 的执行：参数校验被拒的路径走不到这里，不计耗时。
        # 失败路径（AppError）也记耗时 —— 上游超时或 5xx 正是要从这个数看出来的。
        # 同时记下调用前已有结果的 id：只有真产出新结果才即时落库，无关工具不白写一次库。
        before = [item.get("result_id") for item in context.results]
        started = time.monotonic()
        try:
            result = await handler(request)
        except UnauthorizedError:
            # 身份失效必须向上抛：伪装成普通工具失败会让对话继续跑在一个无效会话上。
            raise
        except AppError as exc:
            call["status"] = "failed"
            call["error_code"] = exc.code
            call["upstream_code"] = getattr(exc, "upstream_code", None)
            call["duration_ms"] = round((time.monotonic() - started) * 1000)
            # 把上游真实原因交给模型，而不是一句通用文案 —— 否则模型无从判断缺什么，
            # 只能把问题推回给用户。400/404/422 是「参数不对」，模型可自纠（如换链重查）；
            # 429 与 5xx 属于「稍后重试」，不要引导模型去改参数。
            retryable = exc.status_code not in (400, 404, 422)
            action = "稍后重试" if retryable else "修正参数后重试"
            # ensure_ascii=False：msg 含中文与币种名，转义成 \uXXXX 会白白放大占用的预算。
            return ToolMessage(
                content=json.dumps(
                    {
                        "status": "failed",
                        "error_code": exc.code,
                        "message": exc.message,
                        "hint": f"{action}；不得编造结果",
                    },
                    ensure_ascii=False,
                ),
                tool_call_id=request.tool_call["id"],
                status="error",
            )
        call["status"] = "ok"
        call["duration_ms"] = round((time.monotonic() - started) * 1000)
        # 这次调用新产出的结果 id：前端据此把数据挂到对应工具条目下。
        # 只记新增 —— 合并类工具会把被并入的结果从列表里删掉，消失的不属于这次产出。
        produced = [item.get("result_id") for item in context.results]
        call["result_ids"] = [rid for rid in produced if rid not in before]
        # 产出了新结果就立即落库并唤醒订阅端：卡片不必等 1 秒的落库刻度。
        # 落库与通知的顺序由注入的回调保证，这里只负责「有新结果就喊一声」。
        if produced != before:
            await context.persist()
        return result
