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
    description="""查询当前钱包已收录代币清单的余额概览。
    能力限制：仅返回预收录代币，无法查询合约外未收录代币、部分NFT；每轮请求都会重新拉取链上数据，
    不存在缓存，每次询问都必须重新调用，不得沿用对话历史中的余额。
    适用：用户想查看整体持仓概览、全部收录代币汇总，没有指定查询某一个具体币种。
    禁止使用：用户明确点名单个/多个代币（如查USDC余额、ETH持仓），
    此时必须调用 get_my_token_balance，不要使用本概览工具。""",
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
    description="""查询本人某个币种的余额，只在用户说「我的/持有/余额」或问「我有多少 X」时使用。
    只问「X 多少钱」应改用 get_token_price。BERA/WBERA 独立，不猜币种，不扩大范围。
    每次询问都必须重新调用，不得沿用对话历史中的余额。""",
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
    description="汇总当前对话已有的结果引用，后端去重求和，模型不填写金额；"
    "只在用户要求合并前几次结果时使用。",
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
