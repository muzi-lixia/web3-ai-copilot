"""HTTP 成功与错误均采用项目 code/msg/data 格式，保留真实状态码。"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.core.exceptions import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)


# 业务码独立于 HTTP 状态，保持稳定；内部字符串码继续供日志与 SSE 使用。
ERROR_CODES = {
    "invalid_address": 40001,
    "unsupported_chain": 40002,
    "nonce_invalid": 40003,
    "unauthorized": 40101,
    "access_expired": 40103,
    "forbidden": 40301,
    "not_found": 40401,
    "method_not_allowed": 40501,
    "chat_busy": 40901,
    "validation_error": 42201,
    "model_context_too_large": 42202,
    "rate_limited": 42901,
    "internal_error": 50001,
    "upstream_error": 50201,
    "model_unavailable": 50202,
    "model_empty_response": 50203,
    "model_stream_interrupted": 50204,
    "model_timeout": 50205,
    "chat_unavailable": 50301,
}


def error_response(status: int, code: str, message: str, *, headers=None, errors=None) -> JSONResponse:
    """错误同样使用 code/msg/data，不把失败伪装成 HTTP 200；不回显敏感输入。"""
    body = {
        "code": ERROR_CODES.get(code, status * 100 + 99),
        "msg": message,
        "data": {"errors": errors} if errors is not None else None,
    }
    response_headers = {"Cache-Control": "no-store", **(headers or {})}
    if status == 401:
        response_headers.setdefault("WWW-Authenticate", "Bearer")
    return JSONResponse(status_code=status, content=body, headers=response_headers)


def register_exception_handlers(app: FastAPI) -> None:
    """所有 HTTP 异常走同一结构；SSE 建连后错误保留事件格式。"""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError):
        """业务异常保留明确状态码和公开说明。"""
        logger.warning("http.business_error", extra={"error_code": exc.code})
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError):
        """列出参数位置与原因，省略可能包含凭据的 input 和 ctx。"""
        errors = [
            {"location": list(item["loc"]), "message": item["msg"], "code": item["type"]}
            for item in exc.errors()
        ]
        return error_response(422, "validation_error", "请求参数校验失败", errors=errors)

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException):
        """404、405 等框架错误也遵循统一契约，并保留 Allow 等必要头。"""
        code = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized"}.get(
            exc.status_code, "http_error"
        )
        return error_response(exc.status_code, code, str(exc.detail), headers=exc.headers)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        """内部异常记堆栈，对外仅返回安全的通用说明。"""
        logger.exception("http.internal_error")
        return error_response(500, "internal_error", "服务内部错误")
