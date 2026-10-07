"""统一异常体系 + FastAPI 异常处理器。

原则：业务层只抛 AppError 的子类，不直接抛 HTTPException —— service 层不依赖 HTTP。
响应体统一为 {"error": {"code": ..., "message": ...}}，前端 client.ts 拦截器按这个结构解析。
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
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
    """外部依赖失败：RPC 全部节点不可用 / 行情源全部不可用且无缓存可降级。"""

    status_code = 502
    code = "upstream_error"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ForbiddenError(AppError):
    """已登录，但访问的资源不属于当前身份。"""

    status_code = 403
    code = "forbidden"


class UnsupportedChainError(AppError):
    """请求的 chain_id 不在链注册表里。"""

    status_code = 400
    code = "unsupported_chain"


class UnauthorizedError(AppError):
    """未登录，或 token 缺失 / 过期 / 非法。"""

    status_code = 401
    code = "unauthorized"


class NonceInvalidError(AppError):
    """nonce 不存在或已过期（超时，或同一个 nonce 被重复提交）。"""

    status_code = 400
    code = "nonce_invalid"


class SignatureInvalidError(AppError):
    """签名无法解析，或反推出的地址与请求地址不一致。"""

    status_code = 401
    code = "signature_invalid"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        logger.warning("AppError [%s] %s", exc.code, exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        """把 FastAPI 默认的 {"detail": [...]} 拉回统一错误结构。

        不加这个处理器，入参校验失败会返回另一种形状的响应体，
        前端的错误拦截器只认 {"error": {...}}，会解析失败并丢掉真实原因。
        """
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        detail = str(first.get("msg", "参数不合法"))
        message = f"{field}: {detail}" if field else detail
        logger.warning("ValidationError %s", message)
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error", "message": message}},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "服务内部错误"}},
        )
