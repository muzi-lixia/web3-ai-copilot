"""JSON 日志和请求关联。仅输出白名单元数据；不记录正文、资产、凭证和异常原文。"""

import json
import logging
import re
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import uuid4

trace_id = ContextVar("trace_id", default="")
span_id = ContextVar("span_id", default="")
parent_span_id = ContextVar("parent_span_id", default="")
service_name = ContextVar("service", default="backend")
FIELDS = frozenset(
    {
        "turn_id",
        "tool",
        "model",
        "provider",
        "duration_ms",
        "status",
        "error_code",
        "http_status",
        "method",
        "route",
        "prompt_tokens",
        "completion_tokens",
        "cache_hit",
        "chain_id",
        "result_count",
        "exception_type",
    }
)


class JsonFormatter(logging.Formatter):
    """旧日志插值不进入输出，避免业务异常及第三方 SDK 意外输出敏感数据。"""

    def format(self, record):
        event = str(record.msg)
        if not re.fullmatch(r"[a-z][a-z0-9_.]{0,80}", event):
            event = "application.log"
        body = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": service_name.get(),
            "event": event,
            "logger": record.name,
            "trace_id": trace_id.get(),
            "span_id": span_id.get(),
            "parent_span_id": parent_span_id.get(),
        }
        for key in FIELDS:
            value = getattr(record, key, None)
            if isinstance(value, (str, int, float, bool)):
                if isinstance(value, str) and (
                    len(value) > 160 or "0x" in value or "Bearer " in value or "://" in value
                ):
                    continue
                body[key] = value
        if record.exc_info and record.exc_info[0]:
            body["exception_type"] = record.exc_info[0].__name__
        return json.dumps(body, ensure_ascii=False)


def setup_logging(level="INFO"):
    """输出到标准输出，部署环境负责收集、轮转和保存；不另建追踪平台。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("httpx", "httpcore", "web3", "urllib3", "sqlalchemy.engine", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def get_logger(name):
    return logging.getLogger(name)


@contextmanager
def span(event, **fields):
    """子阶段沿用 trace_id；退出时记录耗时和状态，异常只记录安全错误码。"""
    parent = parent_span_id.set(span_id.get())
    current = span_id.set(uuid4().hex[:16])
    started, status, error = time.monotonic(), "success", None
    try:
        yield
    except BaseException as exc:
        status = "cancelled" if type(exc).__name__ == "CancelledError" else "failed"
        error = getattr(exc, "code", type(exc).__name__)
        raise
    finally:
        get_logger(__name__).info(
            event,
            extra={
                **fields,
                "status": status,
                "error_code": error,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            },
        )
        span_id.reset(current)
        parent_span_id.reset(parent)


class TraceMiddleware:
    """纯 ASGI 中间件不缓冲 SSE；流结束后才记录整个 HTTP 请求耗时。"""

    def __init__(self, app, service="backend"):
        self.app, self.service = app, service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        incoming = headers.get(b"x-trace-id", b"").decode("ascii", errors="ignore")
        parent = headers.get(b"x-parent-span-id", b"").decode("ascii", errors="ignore")
        tokens = [
            (trace_id, trace_id.set(incoming if re.fullmatch(r"[a-f0-9]{32}", incoming) else uuid4().hex)),
            (span_id, span_id.set(uuid4().hex[:16])),
            (parent_span_id, parent_span_id.set(parent if re.fullmatch(r"[a-f0-9]{16}", parent) else "")),
            (service_name, service_name.set(self.service)),
        ]
        started, status = time.monotonic(), 500

        async def traced_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-trace-id", trace_id.get().encode()),
                    (b"cache-control", b"no-store"),
                ]
            await send(message)

        try:
            await self.app(scope, receive, traced_send)
        finally:
            get_logger(__name__).info(
                "http.completed",
                extra={
                    "method": scope["method"],
                    "route": getattr(scope.get("route"), "path", "unmatched"),
                    "http_status": status,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                },
            )
            for variable, token in reversed(tokens):
                variable.reset(token)
