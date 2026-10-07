"""路由依赖。"""

from fastapi import Header

from app.core.errors import UnauthorizedError
from app.services import auth_service


async def get_current_address(authorization: str | None = Header(default=None)) -> str:
    """从 `Authorization: Bearer <token>` 解析出当前登录地址。

    作为路由依赖使用：`address: str = Depends(get_current_address)`。
    需要登录的接口挂上它即可，token 无效会直接返回 401，业务代码里不必再判。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("缺少登录凭证")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise UnauthorizedError("缺少登录凭证")

    return auth_service.decode_token(token)
