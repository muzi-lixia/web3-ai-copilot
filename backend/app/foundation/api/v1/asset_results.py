"""基础服务 HTTP 接口；业务逻辑由 modules 提供。"""

from fastapi import APIRouter, Request, Response
from pydantic import Field, model_validator

from app.common.schemas import Address
from app.core.http.responses import success
from app.foundation.api.dependencies import AgentQueryIdentity, UserIdentity
from app.foundation.api.v1.schemas import Input

router = APIRouter()


class Query(Input):
    """资产查询条件：链、币种与计价货币。

    symbol 可为注册符号或代币合约地址；合约地址必须同时给出 chain_id（见下方校验）。
    """

    chain_id: int | None = Field(default=None, gt=0, strict=True)
    symbol: str | None = Field(default=None, min_length=1, max_length=64)
    currency: str = Field(default="USD", pattern="^(USD|CNY)$")

    @model_validator(mode="after")
    def contract_requires_chain(self):
        """合约地址脱离链没有意义，缺 chain_id 直接拒绝，避免歧义查询。"""
        if self.symbol and self.symbol.startswith("0x") and self.chain_id is None:
            raise ValueError("代币合约必须同时指定网络")
        return self


class Aggregate(Input):
    """按结果 ID 汇总：一次最多 10 条，只允许汇总本人结果。"""

    result_ids: list[str] = Field(min_length=1, max_length=10)
    currency: str = Field(default="USD", pattern="^(USD|CNY)$")


class TokenResolution(Input):
    """解析代币合约元数据的入参：链 ID 与合约地址必填。"""

    chain_id: int = Field(gt=0)
    contract: Address


@router.post("/me/balance-results", status_code=201, tags=["资产查询"], summary="创建本人资产查询结果")
async def balances(body: Query, request: Request, current: AgentQueryIdentity):
    """创建一次资产查询结果。按用户限流，返回的 result_id 供后续汇总/引用。"""
    services = request.app.state.services
    # 先限流再查询：资产查询会打链上 RPC，突发流量必须在入口拦住。
    await services.auth.limit("assets:" + current.user_id, services.settings.request_limit_per_minute)
    return success(await services.results.query(current, body.chain_id, body.symbol, body.currency))


@router.get("/me/balance-results/{result_id}", tags=["资产查询"], summary="读取本人资产查询结果")
async def result(result_id: str, request: Request, current: UserIdentity):
    """按 ID 读本人查询结果，返回信封 + body；结果按 user_id 归属，读他人结果会 404。"""
    return success(await request.app.state.services.results.read(current.user_id, result_id))


@router.post("/me/aggregations", status_code=201, tags=["资产查询"], summary="去重汇总本人查询结果")
async def aggregate(body: Aggregate, request: Request, current: AgentQueryIdentity):
    """把多条查询结果去重汇总成一份持仓视图，同样受用户级限流约束。"""
    await request.app.state.services.auth.limit(
        "assets:" + current.user_id, request.app.state.services.settings.request_limit_per_minute
    )
    return success(
        await request.app.state.services.results.aggregate(current.user_id, body.result_ids, body.currency)
    )


@router.post("/tokens/resolutions", tags=["资产查询"], summary="解析代币合约元数据")
async def resolve_token(body: TokenResolution, request: Request, current: UserIdentity):
    """代币地址不是钱包查询目标，先验证公开合约能力，再允许 Agent 作为资产标识。"""
    from app.modules.asset.custom_token import metadata

    services = request.app.state.services
    # 既要验证合约本身，也要按用户限流，避免被拿来批量探测任意地址。
    await services.auth.limit("metadata:" + current.user_id, services.settings.request_limit_per_minute)
    return success(await metadata(body.chain_id, body.contract, services.resources))


@router.delete("/me/balance-results", status_code=204, tags=["资产查询"], summary="删除本人资产查询结果")
async def delete_results(body: Aggregate, request: Request, current: UserIdentity):
    """删除本人指定的查询结果；同样只允许操作归属自己的结果。"""
    await request.app.state.services.results.delete(current.user_id, body.result_ids)
    return Response(status_code=204)
