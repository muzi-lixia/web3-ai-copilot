"""当前用户语义的认证、资产结果和公开链接口；Agent 也通过这些 HTTP 契约调用。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.dependencies import BusinessResourcesDep
from app.api.responses import success
from app.common.schemas import Address
from app.core.exceptions import UnauthorizedError
from app.infrastructure.blockchain.chains import CHAINS
from app.modules.asset.tokens import tokens_for

router = APIRouter()
bearer = HTTPBearer(auto_error=False)


class Input(BaseModel):
    """请求体基类：统一禁止多余字段，避免前端多传的字段被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class Challenge(Input):
    """申请签名消息：只需要用户钱包地址。"""

    address: Address


class Login(Input):
    """提交签名：message 为原样回传的挑战消息，signature 为 65 字节十六进制签名。"""

    message: str = Field(min_length=1, max_length=2048)
    signature: str = Field(pattern=r"^0x[a-fA-F0-9]{130}$")


class Refresh(Input):
    """续期请求：只承载刷新凭证本身。"""

    refresh_token: str = Field(min_length=32, max_length=256)


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


async def identity(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
):
    """取当前用户身份；缺凭证直接 401，是各受保护路由的统一入口依赖。"""
    if not credentials:
        raise UnauthorizedError("缺少登录凭证")
    return await request.app.state.services.auth.authenticate(credentials.credentials)


IdentityDep = Annotated[object, Depends(identity)]


async def login_limit(request: Request):
    """按来源 IP 限流登录相关接口，防止挑战/签名接口被刷。"""
    auth = request.app.state.services.auth
    await auth.limit(
        "login:" + (request.client.host if request.client else "unknown"), auth.settings.auth_rate_per_minute
    )


@router.post(
    "/auth/challenges",
    status_code=201,
    dependencies=[Depends(login_limit)],
    tags=["身份与凭证"],
    summary="获取钱包登录签名消息",
    openapi_extra={"security": [], "x-auth-description": "公开访问 · 限流"},
)
async def challenge(body: Challenge, request: Request):
    """签发一次性签名消息。挑战短期有效且只能消费一次，防重放。"""
    return success(await request.app.state.services.auth.challenge(body.address))


@router.post(
    "/auth/tokens",
    dependencies=[Depends(login_limit)],
    tags=["身份与凭证"],
    summary="验证钱包签名并签发凭证",
    openapi_extra={"security": [], "x-auth-description": "公开访问 · 验证签名"},
)
async def login(body: Login, request: Request):
    """验证签名并签发凭证；失败统一记一次审计，不区分具体失败原因对外暴露。"""
    try:
        return success(await request.app.state.services.auth.login(body.message, body.signature))
    except Exception:
        await request.app.state.services.auth.audit("login", "failed")
        raise


@router.post(
    "/auth/refresh",
    dependencies=[Depends(login_limit)],
    tags=["身份与凭证"],
    summary="轮换刷新凭证并续期",
    openapi_extra={"security": [], "x-auth-description": "Refresh Token（请求体）"},
)
async def refresh(body: Refresh, request: Request):
    """用刷新凭证换新的一对令牌，旧的随即可作废（轮换）。"""
    return success(await request.app.state.services.auth.refresh(body.refresh_token))


@router.delete("/auth/session", status_code=204, tags=["身份与凭证"], summary="注销当前登录会话")
async def logout(request: Request, current: IdentityDep):
    """注销当前会话：吊销凭证，之后的请求会被判为未授权。"""
    await request.app.state.services.auth.logout(current)
    return Response(status_code=204)


@router.get("/users/me", tags=["身份与凭证"], summary="读取当前用户身份")
async def me(current: IdentityDep, resources: BusinessResourcesDep):
    """返回当前用户的地址与内部用户 ID，并标明是否具备知识库管理权限。"""
    return success(
        {
            "address": current.address,
            "user_id": current.user_id,
            "is_admin": current.address.lower() in resources.settings.admin_address_set,
        }
    )


@router.get("/auth/identity", tags=["身份与凭证"], summary="读取无地址身份投影")
async def introspect(current: IdentityDep):
    """公开给持凭证调用方的无地址身份投影；基础服务仍独立鉴权每个工具请求。"""
    return success({"user_id": current.user_id, "session_id": current.session_id})


@router.post("/me/balance-results", status_code=201, tags=["资产查询"], summary="创建本人资产查询结果")
async def balances(body: Query, request: Request, current: IdentityDep):
    """创建一次资产查询结果。按用户限流，返回的 result_id 供后续汇总/引用。"""
    services = request.app.state.services
    # 先限流再查询：资产查询会打链上 RPC，突发流量必须在入口拦住。
    await services.auth.limit("assets:" + current.user_id, services.settings.request_limit_per_minute)
    return success(await services.results.query(current, body.chain_id, body.symbol, body.currency))


@router.get("/me/balance-results/{result_id}", tags=["资产查询"], summary="读取本人资产查询结果")
async def result(result_id: str, request: Request, current: IdentityDep):
    """按 ID 读本人查询结果，返回信封 + body；结果按 user_id 归属，读他人结果会 404。"""
    return success(await request.app.state.services.results.read(current.user_id, result_id))


@router.post("/me/aggregations", status_code=201, tags=["资产查询"], summary="去重汇总本人查询结果")
async def aggregate(body: Aggregate, request: Request, current: IdentityDep):
    """把多条查询结果去重汇总成一份持仓视图，同样受用户级限流约束。"""
    await request.app.state.services.auth.limit(
        "assets:" + current.user_id, request.app.state.services.settings.request_limit_per_minute
    )
    return success(
        await request.app.state.services.results.aggregate(current.user_id, body.result_ids, body.currency)
    )


@router.get(
    "/chains",
    tags=["公共能力"],
    summary="读取支持的网络与代币目录",
    openapi_extra={"security": [], "x-auth-description": "匿名可访问；提供凭证时仍校验"},
)
async def chains(request: Request):
    """列出支持的网络与各自代币目录。匿名可访问，但带凭证时仍验证并改用更宽松的用户限流。"""
    auth = request.app.state.services.auth
    credentials = await bearer(request)
    # 匿名也放行，但按来源 IP 用更严的匿名配额；带凭证则按用户使用业务配额。
    if credentials:
        current = await auth.authenticate(credentials.credentials)
        key, limit = "chains:user:" + current.user_id, auth.settings.request_limit_per_minute
    else:
        key = "chains:anonymous:" + (request.client.host if request.client else "unknown")
        limit = auth.settings.anonymous_limit_per_minute
    await auth.limit(key, limit)
    return success(
        [
            {
                "chain_id": c.chain_id,
                "name": c.name,
                "key": c.key,
                "symbols": [t.symbol for t in tokens_for(c.chain_id)],
            }
            for c in CHAINS
        ]
    )


class TokenResolution(Input):
    """解析代币合约元数据的入参：链 ID 与合约地址必填。"""

    chain_id: int = Field(gt=0)
    contract: Address


@router.post("/tokens/resolutions", tags=["资产查询"], summary="解析代币合约元数据")
async def resolve_token(body: TokenResolution, request: Request, current: IdentityDep):
    """代币地址不是钱包查询目标，先验证公开合约能力，再允许 Agent 作为资产标识。"""
    from app.modules.asset.custom_token import metadata

    services = request.app.state.services
    # 既要验证合约本身，也要按用户限流，避免被拿来批量探测任意地址。
    await services.auth.limit("metadata:" + current.user_id, services.settings.request_limit_per_minute)
    return success(await metadata(body.chain_id, body.contract, services.resources))


@router.post("/auth/agent-permits", tags=["身份与凭证"], summary="签发一轮 Agent 执行的委托凭证")
async def agent_permit(request: Request, current: IdentityDep):
    """校验当前用户可用 Agent，并签发覆盖整轮执行的委托凭证。

    用户令牌只需此刻有效：Agent 拿这张凭证跑完整轮取数，令牌在生成期间过期不再让这一轮
    作废。委托凭证本身不能再签发 —— 否则 Agent 可以绕开用户令牌无限续期。
    """
    auth = request.app.state.services.auth
    if current.scope != "user":
        raise UnauthorizedError("委托凭证不能再次签发")
    await auth.limit("agent:" + current.user_id, auth.settings.request_limit_per_minute)
    return success({"permit": auth.issue_agent_permit(current)})


@router.get(
    "/markets/prices",
    tags=["行情"],
    summary="查询单币价格与统计",
    openapi_extra={"security": [], "x-auth-description": "匿名可访问；提供凭证时仍校验"},
)
async def public_price(request: Request, symbol: str, chain_id: int, currency: str = "USD"):
    """查询单币价格与统计。匿名可访问但限流更严；带凭证则按用户限流。"""
    from app.foundation.api import bearer
    from app.modules.market.public import quote

    auth = request.app.state.services.auth
    credentials = await bearer(request)
    # 与 /chains 同策略：匿名放行但配额更严，带凭证则按用户计。
    if credentials:
        current = await auth.authenticate(credentials.credentials)
        key, limit = "public:user:" + current.user_id, auth.settings.request_limit_per_minute
    else:
        key = "public:anonymous:" + (request.client.host if request.client else "unknown")
        limit = auth.settings.anonymous_limit_per_minute
    await auth.limit(key, limit)
    return success(await quote(chain_id, symbol, currency.upper(), request.app.state.services.resources))


@router.delete("/me/balance-results", status_code=204, tags=["资产查询"], summary="删除本人资产查询结果")
async def delete_results(body: Aggregate, request: Request, current: IdentityDep):
    """删除本人指定的查询结果；同样只允许操作归属自己的结果。"""
    await request.app.state.services.results.delete(current.user_id, body.result_ids)
    return Response(status_code=204)
