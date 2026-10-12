"""公开读工具：链目录与单币行情，不涉及本人资产结果表。"""

import json

from langchain.tools import ToolRuntime, tool

from app.agent.context import RunContext
from app.agent.tools.base import BalanceArguments, ChainArguments
from app.agent.tools.envelope import make_result
from app.core.logging import span


@tool(
    args_schema=ChainArguments,
    description="查询平台支持的链 ID 与代币清单，回答「支持哪些链/币」这类问题；不查本人持仓。",
)
async def get_chain_info(runtime: ToolRuntime[RunContext], chain_id: int | None = None):
    """读取公开网络配置。

    Args:
        runtime: 带用户凭证的 HTTP 执行上下文。
        chain_id: 可选过滤网络。
    Returns:
        公开链配置，不包括钱包或 RPC 凭据。
    """
    data = await runtime.context.client.request("GET", "/chains", runtime.context.token)
    return json.dumps([c for c in data if chain_id is None or c["chain_id"] == chain_id])


@tool(
    args_schema=BalanceArguments,
    description="查询公开代币行情价格，回答「X 多少钱」。这是市场价格，不是用户余额；"
    "问「我有多少 X」应改用 get_my_token_balance。"
    "每次询问都必须重新调用，不得沿用对话历史中的行情。",
)
async def get_token_price(
    symbol: str, runtime: ToolRuntime[RunContext], chain_id: int | None = None, currency: str = "USD"
):
    """查询公开行情。

    Args:
        symbol: 精确币种符号。
        runtime: 服务端可信上下文。
        chain_id: 可选网络，省略使用默认网络。
        currency: 公开报价单位，USD 或 CNY。
    Returns:
        公开报价及来源和时间，不包含持仓。
    """
    cid = chain_id or runtime.context.client.default_chain_id
    with span("tool.completed", tool="get_token_price"):
        data = await runtime.context.client.request(
            "GET",
            "/markets/prices",
            runtime.context.token,
            params={"chain_id": cid, "symbol": symbol, "currency": currency},
        )
        # 行情是公开数据，不进基础服务的资产结果表，因此就地装成一条 price 结果：
        # 只给前端挂卡片用，没有归属引用，也不参与汇总。
        make_result(
            runtime.context,
            "price",
            {"chain_id": cid, "token": data},
            source_desc=f"公开行情 · {data.get('source') or '未知来源'}",
            currency=currency,
            sources=[{"name": data.get("source"), "at": data.get("updated_at")}],
            queried_at=data.get("queriedAt"),
        )
        # 行情要由模型直接作答，正文数据照旧全部交给模型，不进白名单。
        return json.dumps(data, ensure_ascii=False)
