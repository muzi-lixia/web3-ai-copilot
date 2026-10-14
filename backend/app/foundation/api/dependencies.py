"""HTTP 依赖只从应用状态获取服务，不引入模块级运行时单例。"""

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.infrastructure.resources import BusinessResources
from app.modules.identity.service import AuthService, Identity


def get_auth_service(request: Request) -> AuthService:
    """取得处理当前请求的应用鉴权服务，不创建新的 nonce 池或限流状态。"""
    return request.app.state.services.auth


def get_business_resources(request: Request) -> BusinessResources:
    """所有业务路由使用当前应用的配置与缓存，不读取另一份全局配置。"""
    return request.app.state.services.resources


BusinessResourcesDep = Annotated[BusinessResources, Depends(get_business_resources)]


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


bearer = HTTPBearer(auto_error=False, scheme_name="WalletBearer")


async def get_authenticated_identity(
    auth: AuthServiceDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Identity:
    """使用标准 Bearer 安全方案验证凭证并返回完整身份；OpenAPI 同步标注所需认证。"""
    if credentials is None or not credentials.credentials.strip():
        raise UnauthorizedError("缺少登录凭证")
    return await auth.authenticate(credentials.credentials)


AuthenticatedIdentity = Annotated[Identity, Depends(get_authenticated_identity)]


async def get_current_identity(current: AuthenticatedIdentity) -> Identity:
    """普通接口默认只接受用户凭证，Agent 必须由接口显式开放。"""
    if current.scope != "user":
        raise ForbiddenError("该接口仅允许用户凭证访问")
    return current


CurrentIdentity = Annotated[Identity, Depends(get_current_identity)]
UserIdentity = CurrentIdentity


async def get_agent_query_identity(current: AuthenticatedIdentity) -> Identity:
    """仅供明确允许 Agent 的查询接口使用。"""
    if current.scope not in ("user", "agent"):
        raise ForbiddenError("凭证不具备查询权限")
    return current


AgentQueryIdentity = Annotated[Identity, Depends(get_agent_query_identity)]


async def get_current_address(current: CurrentIdentity) -> str:
    """只要地址的接口走这个依赖，取不到 user_id 与会话 ID。"""
    return current.address


CurrentAddress = Annotated[str, Depends(get_current_address)]


async def get_admin_identity(current: CurrentIdentity, resources: BusinessResourcesDep) -> Identity:
    """知识库写操作只对白名单地址开放；白名单留空时无人通过。"""
    if current.address.lower() not in resources.settings.admin_address_set:
        raise ForbiddenError("当前账号没有知识库管理权限")
    return current


AdminIdentity = Annotated[Identity, Depends(get_admin_identity)]
