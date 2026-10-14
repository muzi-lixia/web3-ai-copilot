"""数据库观测仅记录时延、状态与 SQLSTATE，不记录 SQL、参数或连接串。"""

import time
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

LOGGER = get_logger(__name__)


class ObservedSession(AsyncSession):
    @asynccontextmanager
    async def begin(self):
        started = time.monotonic()
        status, pool_ms, commit_started = "success", 0.0, None
        try:
            async with super().begin() as transaction:
                # 提前取连接，准确度量连接池获取（含健康检查/新建连接）等待。
                pool_started = time.monotonic()
                await self.connection()
                pool_ms = (time.monotonic() - pool_started) * 1000
                yield transaction
                commit_started = time.monotonic()
        except BaseException:
            status = "failed"
            raise
        finally:
            elapsed = (time.monotonic() - started) * 1000
            LOGGER.log(
                30 if status == "failed" or elapsed >= 500 or pool_ms >= 100 else 20,
                "database.transaction",
                extra={"status": status, "duration_ms": round(elapsed, 2),
                       "pool_wait_ms": round(pool_ms, 2),
                       "commit_ms": round((time.monotonic() - commit_started) * 1000, 2)
                       if commit_started is not None else 0.0},
            )


def instrument_engine(engine):
    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def before(connection, cursor, statement, parameters, context, executemany):
        context._copilot_started = time.monotonic()

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def after(connection, cursor, statement, parameters, context, executemany):
        elapsed = (time.monotonic() - context._copilot_started) * 1000
        if elapsed >= 500:
            LOGGER.warning("database.slow_query", extra={"duration_ms": round(elapsed, 2)})

    @event.listens_for(engine.sync_engine, "handle_error")
    def failed(context):
        error = context.original_exception
        LOGGER.error("database.query_failed", extra={
            "exception_type": type(error).__name__,
            "sqlstate": getattr(error, "sqlstate", None),
        })
