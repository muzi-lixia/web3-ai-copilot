"""本人资产工具：余额查询交给 assets 子图，结果汇总交给基础服务聚合。"""

import json

from langchain.tools import ToolRuntime, tool
from langgraph.constants import CONFIG_KEY_CHECKPOINTER
from pydantic import Field, model_validator

from app.agent.assets import ASSETS_GRAPH
from app.agent.context import RunContext
from app.agent.tools.base import Arguments, PricedArguments
from app.core.logging import span


class AssetsArguments(PricedArguments):
    """余额查询参数：币种可多可空，空即整体概览。"""

    symbols: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="用户点到的币种符号或代币合约地址；没有点名币种时留空，表示查全部持仓概览",
    )

    @model_validator(mode="after")
    def contract_requires_chain(self):
        """代币合约脱离网络无法解析，必须在参数阶段拒绝，而不是留给下游报错。"""
        if any(item.startswith("0x") for item in self.symbols) and self.chain_id is None:
            raise ValueError("代币合约必须同时指定网络")
        return self


class AggregateArguments(Arguments):
    """汇总参数：只接收结果引用 ID，金额由后端计算，模型填不了。"""

    currency: str = Field(default="USD", pattern="^(USD|CNY)$")
    result_ids: list[str] = Field(min_length=1, max_length=10, description="当前对话已产生的结果引用 ID")


@tool(
    args_schema=AssetsArguments,
    description="""查询本人钱包的余额：点名了币种就逐个查，没点名就查全部持仓概览。
                适用场景：用户询问「我的余额」「我有多少 X」「我持有多少X」「我一共有多少资产」。
                参数约定：用户点到的币种填进 symbols，一个也没点到时留空，不要为此追问。
                禁止场景：仅询问「X 多少钱」时，改用 get_token_price；
                询问平台支持哪些链和币时，改用 get_chain_info。
                特殊规则：BERA与WBERA相互独立，不会自动互相转换；严格按用户点到的币种查询，不猜测，不扩大范围。
                每次调用实时拉取链上数据，不得沿用对话历史中的旧余额。""",
)
async def query_assets(
    runtime: ToolRuntime[RunContext],
    symbols: list[str] | None = None,
    chain_id: int | None = None,
    currency: str = "USD",
):
    """查询本人余额：一次调用取回一个或多个币种，也可以取整体概览。

    Args:
        runtime: 上下文
        symbols: 用户点到的币种符号或合约地址；留空表示查全部已登记代币的概览。
        chain_id: 网络；点名币种时省略则用部署默认网络。
        currency: 估值展示单位，USD 或 CNY。
    Returns:
        每条结果的摘要与取数失败说明；真实数值由前端读取基础服务结果。
    """
    state = await ASSETS_GRAPH.ainvoke(
        {
            "symbols": list(symbols or ()),
            "chain_id": chain_id,
            "currency": currency,
        },
        # 上下文走运行上下文通道，不进 state —— 与主图 create_agent(context_schema=...) 一致。
        context=runtime.context,
        # 子图不落快照：显式切断从父 run 继承来的 checkpointer。
        config={"configurable": {CONFIG_KEY_CHECKPOINTER: None}},
    )
    return state["reply"]


@tool(
    args_schema=AggregateArguments,
    description="""汇总当前对话已存在的查询结果引用，后端完成去重与金额求和，模型不参与金额计算、不填写金额。
                适用场景：用户明确要求合并本次对话前面几次查询到得到的结果。
                约束：仅可传入当前用户、当前对话下有效的result_ids，禁止编造不存在的result_ids。其他未指明汇总结果的场景禁止使用。""",
)
async def aggregate_results(result_ids: list[str], runtime: ToolRuntime[RunContext], currency: str = "USD"):
    """汇总已经生成的业务结果。

    Args:
        result_ids: 必须属于当前用户当前对话，不能凭空编造。
        currency: 汇总展示货币，内部统一按 USD 估值后转换。
        runtime: 当前用户身份及结果引用仓储。
    Returns:
        汇总状态及新的结果引用；基础服务完成去重和计算。
    """
    c = runtime.context
    # 汇总前先校验引用归属：这是防止模型编造 result_id 的闸门（模型编得出 ID，编不出归属关系）。
    await c.repository.check_refs(c.user_id, c.conversation_id, result_ids)
    with span("tool.completed", tool="aggregate_results"):
        meta = await c.client.request(
            "POST", "/me/aggregations", c.token, json={"result_ids": result_ids, "currency": currency}
        )
        await c.repository.add_ref(c.user_id, c.conversation_id, meta["result"]["result_id"])
        # 被合并的中间结果不再单独出卡：前端是一条结果一张卡，留着就会「问一次出三张卡」，
        # 而模型正文只挂第一张，看起来像是挂错了。合并结果本身已经含这些数据。
        # sources 里就是被合并结果的 id（基础服务落库时写入），据此摘除，不新增字段。
        merged = {src.get("id") for src in meta["result"].get("sources") or []}
        c.results[:] = [item for item in c.results if item["result_id"] not in merged]
        c.results.append(meta["result"])
        return json.dumps(meta["summary"], ensure_ascii=False)
