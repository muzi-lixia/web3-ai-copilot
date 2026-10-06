"""统一异常体系 + FastAPI 异常处理器。

原则：业务层只抛 AppError 的子类，不直接抛 HTTPException —— service 层不依赖 HTTP。
响应体统一为 {"error": {"code": ..., "message": ...}}，前端 client.ts 拦截器按这个结构解析。
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    """业务异常基类。"""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class InvalidAddressError(AppError):
    """地址格式非法（非 0x + 40 hex / checksum 校验失败）。"""

    status_code = 400
    code = "invalid_address"


class UpstreamError(AppError):
    """外部依赖失败：RPC 全部节点不可用 / CoinGecko 限流且无缓存可降级。"""

    status_code = 502
    code = "upstream_error"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        logger.warning("AppError [%s] %s", exc.code, exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "服务内部错误"}},
        )
