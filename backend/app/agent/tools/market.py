"""公开读工具：链目录与单币行情，不涉及本人资产结果表。"""

import json

from langchain.tools import ToolRuntime, tool
from pydantic import Field, model_validator

from app.agent.context import RunContext
from app.agent.tools.base import ChainArguments, PricedArguments
from app.agent.tools.envelope import make_result
from app.core.logging import span


class TokenPriceArguments(PricedArguments):
    """指定单个币种的查询参数：在计价参数上追加一个必填符号。"""

    symbol: str = Field(
        min_length=1, max_length=64, description="精确币种符号，例如 BERA 或 WBERA；二者不可混用"
    )

    @model_validator(mode="after")
    def contract_requires_chain(self):
        """代币合约脱离网络无法解析，必须在参数阶段拒绝，而不是留给下游报错。"""
        if self.symbol.startswith("0x") and self.chain_id is None:
            raise ValueError("代币合约必须同时指定网络")
        return self


@tool(
    args_schema=ChainArguments,
    description="查询平台支持的链列表及对应代币清单，回答「支持哪些链/币」这类问题；仅返回平台公开配置信息，不查询用户任何钱包持仓、余额。可选传入chain_id，用于查询指定一条链的信息。",
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
    args_schema=TokenPriceArguments,
    description="""查询代币公开市场行情价格，用于回答「X 多少钱」。该数据为市场价格，不是用户钱包持仓余额。
                禁止场景：用户询问「我有多少 X」「我的 X 余额」时，改用 query_assets。
                每次调用实时拉取最新行情，无缓存，不可复用对话历史内旧行情数据。""",
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
