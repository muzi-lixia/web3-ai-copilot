"""将业务异常与请求校验错误转换成统一 HTTP 响应。"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.infra.logging import get_logger
from app.shared.errors import AppError

logger = get_logger(__name__)


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
        logger.error("Unhandled exception", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "服务内部错误"}},
        )
