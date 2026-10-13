"""资产类查询工具：走基础服务 /me/balance-results，结果落库并出卡片。

信封与 body 由基础服务的 results.py 产出，这里只负责转发并把两份投影分流。
"""

import json

from langchain.tools import ToolRuntime, tool
from pydantic import Field

from app.agent.context import RunContext
from app.agent.tools.base import Arguments, BalanceArguments, OverviewArguments, query
from app.core.logging import span


class AggregateArguments(Arguments):
    """汇总参数：只接收结果引用 ID，金额由后端计算，模型填不了。"""

    currency: str = Field(default="USD", pattern="^(USD|CNY)$")
    result_ids: list[str] = Field(min_length=1, max_length=10, description="当前对话已产生的结果引用 ID")


@tool(
    args_schema=OverviewArguments,
    description="""查询当前钱包预收录代币的余额概览。
                能力限制：仅查询预收录代币，无法查询未收录合约代币、部分NFT；每次调用实时拉取链上数据，无缓存，不能复用历史余额结果。
                适用场景：用户查询全部持仓汇总、整体资产概览，未指定单个或多个代币。
                禁止场景：用户明确点名查询一个/多个指定币种（如查USDC余额、ETH持仓），此时必须调用 get_my_token_balance，禁止调用本工具。""",
)
async def get_my_balance_overview(
    runtime: ToolRuntime[RunContext], chain_id: int | None = None, currency: str = "USD"
):
    """查询本人已登记代币余额概览。

    Args:
        runtime: 框架注入的可信身份及 HTTP 客户端，模型不可填写。
        chain_id: 可选网络；不传查询全部登记网络。
        currency: 估值展示单位，USD 或 CNY。
    Returns:
        查询范围、完整性和结果引用。真实数值由前端读取基础服务结果。
    """
    return await query(runtime, "get_my_balance_overview", {"chain_id": chain_id, "currency": currency})


@tool(
    args_schema=BalanceArguments,
    description="""查询本人指定单个币种的余额。
                适用场景：用户询问「我的余额」「我有多少 X」「我持有多少X」时使用。
                禁止场景：仅询问「X 多少钱」时，改用 get_token_price。查询全部钱包资产汇总概览时，改用 get_my_balance_overview。
                特殊规则：BERA与WBERA相互独立，不会自动互相转换；严格按用户指定币种查询，不猜测，不扩大范围。
                每次调用实时拉取链上数据，不得沿用对话历史中的旧余额。""",
)
async def get_my_token_balance(
    symbol: str, runtime: ToolRuntime[RunContext], chain_id: int | None = None, currency: str = "USD"
):
    """查询指定代币余额，身份只能来自运行上下文。

    Args:
        symbol: 用户明确指定的符号。
        runtime: 可信上下文，不进入模型 schema。
        chain_id: 网络，未明确时先由对话上下文确定。
        currency: 估值展示单位，USD 或 CNY。
    Returns:
        安全元数据和结果引用，不返回余额正文。
    """
    if chain_id is None:
        # 模型没给网络时用部署默认网络，而不是让下游猜。
        chain_id = runtime.context.client.default_chain_id
    return await query(
        runtime,
        "get_my_token_balance",
        {
            "chain_id": chain_id,
            "currency": currency,
            # 合约地址大小写敏感，保持原样；符号统一大写后再去匹配注册表。
            "symbol": symbol.strip() if symbol.startswith("0x") else symbol.strip().upper(),
        },
    )


@tool(
    args_schema=AggregateArguments,
    description="""汇总当前对话已存在的查询结果引用，后端完成去重与金额求和，模型不参与金额计算、不填写金额。
                适用场景：用户明确要求合并本次对话前面几次查询到得到的结果。
                约束：仅可传入当前用户、当前对话下有效的result_ids，禁止编造不存在的result_ids。""",
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
