"""基础服务 HTTP 接口；业务逻辑由 modules 提供。"""

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field

from app.common.schemas import Address
from app.core.http.responses import success
from app.foundation.api.dependencies import AgentQueryIdentity, BusinessResourcesDep, UserIdentity
from app.foundation.api.v1.schemas import Input

router = APIRouter()


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
async def logout(request: Request, current: UserIdentity):
    """注销当前会话：吊销凭证，之后的请求会被判为未授权。"""
    await request.app.state.services.auth.logout(current)
    return Response(status_code=204)


@router.get("/users/me", tags=["身份与凭证"], summary="读取当前用户身份")
async def me(current: UserIdentity, resources: BusinessResourcesDep):
    """返回当前用户的地址与内部用户 ID，并标明是否具备知识库管理权限。"""
    return success(
        {
            "address": current.address,
            "user_id": current.user_id,
            "is_admin": current.address.lower() in resources.settings.admin_address_set,
        }
    )


@router.get("/auth/identity", tags=["身份与凭证"], summary="读取无地址身份投影")
async def introspect(current: AgentQueryIdentity):
    """公开给持凭证调用方的无地址身份投影；基础服务仍独立鉴权每个工具请求。"""
    return success({"user_id": current.user_id, "session_id": current.session_id, "scope": current.scope})


@router.post("/auth/agent-permits", tags=["身份与凭证"], summary="签发一轮 Agent 执行的委托凭证")
async def agent_permit(request: Request, current: UserIdentity):
    """校验当前用户可用 Agent，并签发覆盖整轮执行的委托凭证。

    用户令牌只需此刻有效：Agent 拿这张凭证跑完整轮取数，令牌在生成期间过期不再让这一轮
    作废。委托凭证本身不能再签发 —— 否则 Agent 可以绕开用户令牌无限续期。
    """
    auth = request.app.state.services.auth
    await auth.limit("agent:" + current.user_id, auth.settings.request_limit_per_minute)
    token = auth.issue_agent_permit(current)
    # 基础服务拥有签名密钥；直接返回已签名凭证对应的到期时间。
    import jwt

    claims = jwt.decode(token, auth.settings.jwt_secret, algorithms=["HS256"])
    return success({"permit": token, "expires_at": claims["exp"]})
