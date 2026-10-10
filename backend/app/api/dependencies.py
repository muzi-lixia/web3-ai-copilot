"""HTTP 依赖只从应用状态获取服务，不引入模块级运行时单例。"""

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.exceptions import UnauthorizedError
from app.core.resources import BusinessResources
from app.foundation.auth import AuthService


def get_auth_service(request: Request) -> AuthService:
    """取得处理当前请求的应用鉴权服务，不创建新的 nonce 池或限流状态。"""
    return request.app.state.services.auth


def get_business_resources(request: Request) -> BusinessResources:
    """所有业务路由使用当前应用的配置与缓存，不读取另一份全局配置。"""
    return request.app.state.services.resources


BusinessResourcesDep = Annotated[BusinessResources, Depends(get_business_resources)]


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


bearer = HTTPBearer(auto_error=False, scheme_name="WalletBearer")


async def get_current_address(
    auth: AuthServiceDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    """使用标准 Bearer 安全方案验证 JWT；OpenAPI 同步标注所需认证，不信任自报身份。"""
    if credentials is None or not credentials.credentials.strip():
        raise UnauthorizedError("缺少登录凭证")
    return (await auth.authenticate(credentials.credentials)).address


CurrentAddress = Annotated[str, Depends(get_current_address)]
