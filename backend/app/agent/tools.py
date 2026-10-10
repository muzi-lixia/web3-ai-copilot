"""标准工具是薄 HTTP 适配，不计算余额、不持有钱包地址、不复制真实结果。"""

import json

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.context import RunContext
from app.core.logging import span


class Arguments(BaseModel):
    # ToolNode 在执行阶段加入 runtime；当前框架版本会一并交给 Pydantic 校验。
    # 这里忽略框架注入字段，模型多余参数由 ContextBudget 工具中间件在注入前拒绝。
    model_config = ConfigDict(extra="ignore")


class ChainArguments(Arguments):
    chain_id: int | None = Field(default=None, gt=0, description="链 ID，省略查询已支持网络")


class OverviewArguments(ChainArguments):
    currency: str = Field(default="USD", pattern="^(USD|CNY)$", description="估值计价货币")


class BalanceArguments(OverviewArguments):
    symbol: str = Field(
        min_length=1, max_length=64, description="精确币种符号，例如 BERA 或 WBERA；二者不可混用"
    )

    @model_validator(mode="after")
    def contract_requires_chain(self):
        if self.symbol.startswith("0x") and self.chain_id is None:
            raise ValueError("代币合约必须同时指定网络")
        return self


class AggregateArguments(Arguments):
    currency: str = Field(default="USD", pattern="^(USD|CNY)$")
    result_ids: list[str] = Field(min_length=1, max_length=10, description="当前对话已产生的结果引用 ID")


async def query(runtime, name, payload):
    context = runtime.context
    with span("tool.completed", tool=name):
        meta = await context.client.request("POST", "/me/balance-results", context.token, json=payload)
        await context.repository.add_ref(context.user_id, context.conversation_id, meta["result_id"])
        context.results.append(meta)
        # 模型收到白名单元数据，不使用 content_and_artifact 携带完整资产结果。
        return json.dumps(meta, ensure_ascii=False)


@tool(
    args_schema=OverviewArguments,
    description="查询本人已收录代币清单余额概览，不代表全量资产；每轮重新查询。",
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
    args_schema=BalanceArguments, description="查询本人明确币种余额；BERA/WBERA独立，不猜币种，不扩大范围。"
)
async def get_my_token_balance(
    symbol: str, runtime: ToolRuntime[RunContext], chain_id: int | None = None, currency: str = "USD"
):
    """查询指定代币，身份只能来自运行上下文。

    Args:
        symbol: 用户明确指定的符号。
        runtime: 可信上下文，不进入模型 schema。
        chain_id: 网络，未明确时先由对话上下文确定。
        currency: 估值展示单位，USD 或 CNY。
    Returns:
        安全元数据和结果引用，不返回余额正文。
    """
    if chain_id is None:
        chain_id = runtime.context.client.default_chain_id
    return await query(
        runtime,
        "get_my_token_balance",
        {
            "chain_id": chain_id,
            "currency": currency,
            "symbol": symbol.strip() if symbol.startswith("0x") else symbol.strip().upper(),
        },
    )


@tool(args_schema=ChainArguments, description="查询支持的链 ID 和代币清单，公开数据。")
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


@tool(args_schema=BalanceArguments, description="查询公开代币 USD 行情，价格不是用户余额。")
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
        return json.dumps(data, ensure_ascii=False)


@tool(
    args_schema=AggregateArguments, description="汇总当前对话已有结果引用；后端去重和求和，模型不填写金额。"
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
    await c.repository.check_refs(c.user_id, c.conversation_id, result_ids)
    with span("tool.completed", tool="aggregate_results"):
        meta = await c.client.request(
            "POST", "/me/aggregations", c.token, json={"result_ids": result_ids, "currency": currency}
        )
        await c.repository.add_ref(c.user_id, c.conversation_id, meta["result_id"])
        c.results.append(meta)
        return json.dumps(meta, ensure_ascii=False)


# 注册处只有一处，新增工具直接追加；不存在自定义处理器或反射注册机制。
TOOLS = [get_my_balance_overview, get_my_token_balance, get_token_price, get_chain_info, aggregate_results]
