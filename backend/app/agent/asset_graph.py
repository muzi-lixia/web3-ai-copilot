"""本人资产余额查询子图：规范化参数 → 并发取数 → 汇总成一份交给模型的摘要。

子图内不调用模型：币种由模型填，链路全部由代码走完，每个节点因此都能脱离模型单测。
"""

import asyncio
import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agent.context import RunContext
from app.core.logging import span


class AssetsState(TypedDict, total=False):
    """子图状态：输入是模型填的参数，输出是交给模型的摘要文本。"""

    # 输入——模型填的待查币种；留空表示不点名，按整体概览处理。
    symbols: list[str]
    # 输入——模型填的链 ID；留空表示由下游按符号回退默认链。
    chain_id: int | None
    # 输入——计价货币。
    currency: str
    # 中间态——plan 产出：规范化后的待取数参数清单。
    plans: list[dict]
    # 中间态——fetch 产出：每条成功取数的摘要。
    summaries: list[dict]
    # 中间态——fetch 产出：取数失败的条目及原因。
    failed: list[dict]
    # 输出——settle 产出：交给模型的 JSON 文本。
    reply: str


def plan(state, runtime):
    """把模型填的币种规范成待取数清单；没点名币种就查一次整体概览。

    合约地址大小写敏感、保持原样；符号统一大写后再去匹配注册表，两头空白一并剔掉。
    规范化只在这里做，取数节点拿到的是可直接发出去的参数。
    """
    symbols = [item.strip() for item in state.get("symbols") or () if item.strip()]
    currency = state["currency"]
    chain_id = state.get("chain_id")
    if not symbols:
        # 不带 symbol 即整体概览：chain_id 为空时下游查遍全部登记网络。
        return {"plans": [{"chain_id": chain_id, "currency": currency}]}
    # 点名币种时落到一个网络上：脱离网络无法给同名代币定位，与概览的「全部网络」不同。
    cid = chain_id or runtime.context.client.default_chain_id
    return {
        "plans": [
            {"chain_id": cid, "currency": currency, "symbol": raw if raw.startswith("0x") else raw.upper()}
            for raw in symbols
        ]
    }


async def fetch(state, runtime):
    """并发取数：每条独立登记结果，一条失败不影响其他条。

    全部失败时把第一个原始异常抛出去，与单条查询时的错误处理保持一致 ——
    业务错误码不能被压成一句通用文案。
    """
    # 取出共享的上下文对象
    context = runtime.context
    # 为 state["plans"] 中的每个 payload 创建一个 _fetch_one(context, payload) 协程；使用
    # asyncio.gather 并发执行，return_exceptions=True 表示某个协程抛异常时不中断其他协程，
    # 异常会被当作返回值收集到 outcomes 列表中。
    outcomes = await asyncio.gather(
        *(_fetch_one(context, payload) for payload in state["plans"]), return_exceptions=True
    )
    # 遍历 state["plans"] 和 outcomes，将结果分类
    summaries, failed = [], []
    # 将原始的 plans 和 outcomes 一一配对遍历。strict=True 确保两者长度必须相等，
    # 否则抛 ValueError，防止数据错位。
    for payload, outcome in zip(state["plans"], outcomes, strict=True):
        # 如果 outcome 是异常对象（说明这条查询失败了），就记录失败信息：
        if isinstance(outcome, BaseException):
            failed.append({"symbol": payload.get("symbol"), "reason": _reason(outcome)})
        else:
            summaries.append(outcome)
    # 一条都没成功时抛出第一个原始异常，错误码不被压成通用文案。
    if not summaries:
        raise outcomes[0]
    return {"summaries": summaries, "failed": failed}


async def _fetch_one(context, payload):
    """取一条余额结果：转发基础服务，登记归属引用，并放进本轮展示通道。"""
    with span("tool.completed", tool="query_assets"):
        meta = await context.client.request("POST", "/me/balance-results", context.token, json=payload)
        result_id = meta["result"]["result_id"]
        await context.repository.add_ref(context.user_id, context.conversation_id, result_id)
        context.results.append(meta["result"])
        return meta["summary"]


def _reason(exc):
    """一条取数失败的原因：业务错误给已翻译的 message，其余只给类型名。"""
    return getattr(exc, "message", None) or type(exc).__name__


def settle(state):
    """装成一份给模型的文本；失败的条目一并说明，模型才知道哪些没查到。"""
    payload = {"results": state["summaries"]}
    if state.get("failed"):
        payload["failed"] = state["failed"]
    return {"reply": json.dumps(payload, ensure_ascii=False)}


def build_assets_graph():
    """编译子图：三段固定链路，无模型调用，编译一次即可长期复用。"""
    graph = StateGraph(AssetsState, context_schema=RunContext)
    graph.add_node("plan", plan)
    graph.add_node("fetch", fetch)
    graph.add_node("settle", settle)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "fetch")
    graph.add_edge("fetch", "settle")
    graph.add_edge("settle", END)
    return graph.compile()


ASSETS_GRAPH = build_assets_graph()
