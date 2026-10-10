"""当前用户语义的认证、资产结果和公开链接口；Agent 也通过这些 HTTP 契约调用。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.responses import success
from app.common.schemas import Address
from app.core.exceptions import UnauthorizedError
from app.infrastructure.blockchain.chains import CHAINS
from app.modules.asset.tokens import tokens_for

router = APIRouter()
bearer = HTTPBearer(auto_error=False)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Challenge(Input):
    address: Address


class Login(Input):
    message: str = Field(min_length=1, max_length=2048)
    signature: str = Field(pattern=r"^0x[a-fA-F0-9]{130}$")


class Refresh(Input):
    refresh_token: str = Field(min_length=32, max_length=256)


class Query(Input):
    chain_id: int | None = Field(default=None, gt=0, strict=True)
    symbol: str | None = Field(default=None, min_length=1, max_length=64)
    currency: str = Field(default="USD", pattern="^(USD|CNY)$")

    @model_validator(mode="after")
    def contract_requires_chain(self):
        if self.symbol and self.symbol.startswith("0x") and self.chain_id is None:
            raise ValueError("代币合约必须同时指定网络")
        return self


class Aggregate(Input):
    result_ids: list[str] = Field(min_length=1, max_length=10)
    currency: str = Field(default="USD", pattern="^(USD|CNY)$")


async def identity(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
):
    if not credentials:
        raise UnauthorizedError("缺少登录凭证")
    return await request.app.state.services.auth.authenticate(credentials.credentials)


IdentityDep = Annotated[object, Depends(identity)]


async def login_limit(request: Request):
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
    return success(await request.app.state.services.auth.challenge(body.address))


@router.post(
    "/auth/tokens",
    dependencies=[Depends(login_limit)],
    tags=["身份与凭证"],
    summary="验证钱包签名并签发凭证",
    openapi_extra={"security": [], "x-auth-description": "公开访问 · 验证签名"},
)
async def login(body: Login, request: Request):
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
    return success(await request.app.state.services.auth.refresh(body.refresh_token))


@router.delete("/auth/session", status_code=204, tags=["身份与凭证"], summary="注销当前登录会话")
async def logout(request: Request, current: IdentityDep):
    await request.app.state.services.auth.logout(current)
    return Response(status_code=204)


@router.get("/users/me", tags=["身份与凭证"], summary="读取当前用户身份")
async def me(current: IdentityDep):
    return success({"address": current.address, "user_id": current.user_id})


@router.get("/auth/identity", tags=["身份与凭证"], summary="读取无地址身份投影")
async def introspect(current: IdentityDep):
    """公开给持凭证调用方的无地址身份投影；基础服务仍独立鉴权每个工具请求。"""
    return success({"user_id": current.user_id, "session_id": current.session_id})


@router.post("/me/balance-results", status_code=201, tags=["资产查询"], summary="创建本人资产查询结果")
async def balances(body: Query, request: Request, current: IdentityDep):
    services = request.app.state.services
    await services.auth.limit("assets:" + current.user_id, services.settings.request_limit_per_minute)
    return success(await services.results.query(current, body.chain_id, body.symbol, body.currency))


@router.get("/me/balance-results/{result_id}", tags=["资产查询"], summary="读取本人资产查询结果")
async def result(result_id: str, request: Request, current: IdentityDep):
    return success(await request.app.state.services.results.get(current.user_id, result_id))


@router.post("/me/aggregations", status_code=201, tags=["资产查询"], summary="去重汇总本人查询结果")
async def aggregate(body: Aggregate, request: Request, current: IdentityDep):
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
    auth = request.app.state.services.auth
    credentials = await bearer(request)
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
    chain_id: int = Field(gt=0)
    contract: Address


@router.post("/tokens/resolutions", tags=["资产查询"], summary="解析代币合约元数据")
async def resolve_token(body: TokenResolution, request: Request, current: IdentityDep):
    """代币地址不是钱包查询目标，先验证公开合约能力，再允许 Agent 作为资产标识。"""
    from app.modules.asset.custom_token import metadata

    services = request.app.state.services
    await services.auth.limit("metadata:" + current.user_id, services.settings.request_limit_per_minute)
    return success(await metadata(body.chain_id, body.contract, services.resources))


@router.post("/auth/agent-permits", tags=["身份与凭证"], summary="校验 Agent 使用权限")
async def agent_permit(request: Request, current: IdentityDep):
    auth = request.app.state.services.auth
    await auth.limit("agent:" + current.user_id, auth.settings.request_limit_per_minute)
    return success({"allowed": True})


@router.get(
    "/markets/prices",
    tags=["行情"],
    summary="查询单币价格与统计",
    openapi_extra={"security": [], "x-auth-description": "匿名可访问；提供凭证时仍校验"},
)
async def public_price(request: Request, symbol: str, chain_id: int, currency: str = "USD"):
    from app.foundation.api import bearer
    from app.modules.market.public import quote

    auth = request.app.state.services.auth
    credentials = await bearer(request)
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
    await request.app.state.services.results.delete(current.user_id, body.result_ids)
    return Response(status_code=204)
