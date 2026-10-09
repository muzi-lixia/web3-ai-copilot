"""PostgreSQL 单实例租约；只负责数据库锁，不负责取消任务或决定业务终态。"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.exceptions import ChatUnavailableError

LOCK_KEY = 764031289


class RuntimeLock:
    """通过 PostgreSQL 会话级 advisory lock 限制一个数据库只有一个聊天运行时。

    锁属于专用连接，而非某次事务；连接必须保持打开。failed 是失效闩锁，
    检测失败后不自动重新获取，以防旧实例和新实例同时认为自己可写。
    """

    def __init__(self, engine: AsyncEngine):
        """登记引擎和专用连接状态；_guard 串行化并发健康检查，避免同一连接同时查询。"""
        self.engine = engine
        self.connection: AsyncConnection | None = None
        self.failed = False
        self._guard = asyncio.Lock()

    async def acquire(self) -> None:
        """建立专用连接并尝试非阻塞抢锁，失败时立即清理连接。

        pg_try_advisory_lock 返回 false 表示其他实例占用；commit 不释放会话级锁。
        固定锁键属于同一数据库的全部用户，不能按用户分别取锁。
        """
        if self.connection is not None:
            raise RuntimeError("运行锁已经取得，不能重复启动")
        self.failed = False
        try:
            self.connection = await self.engine.connect()
            held = await self.connection.scalar(text(f"SELECT pg_try_advisory_lock({LOCK_KEY})"))
            await self.connection.commit()
            if not held:
                raise RuntimeError("Copilot 只支持一个实例、一个 worker，已有进程持有运行锁")
        except BaseException:
            await self.close()
            raise

    async def verify(self) -> None:
        """未启动或连接失效时拒绝业务；不自动重新抢锁，避免旧实例继续覆写新实例数据。"""
        async with self._guard:
            if self.failed or self.connection is None:
                raise ChatUnavailableError("聊天运行时未启动或运行锁失效")
            try:
                async with asyncio.timeout(3):
                    if self.connection.invalidated:
                        raise RuntimeError("Lock connection unavailable")
                    held = await self.connection.scalar(
                        text(f"""SELECT EXISTS (
                        SELECT 1 FROM pg_locks WHERE locktype='advisory'
                        AND pid=pg_backend_pid() AND classid=0 AND objid={LOCK_KEY}
                        AND objsubid=1 AND granted)""")
                    )
                    await self.connection.commit()
                    if not held:
                        raise RuntimeError("Runtime lock lost")
            except Exception as exc:
                self.failed = True
                raise ChatUnavailableError("聊天运行锁已失效，请重启服务") from exc

    async def close(self) -> None:
        """先清理事务，再显式解锁并关闭专用连接。

        连接失效时无法发解锁 SQL，由 PostgreSQL 在会话断开后释放；出错会
        将连接作废，避免仍持锁的连接被归还连接池复用。
        """
        connection, self.connection = self.connection, None
        if connection is None:
            return
        try:
            await connection.rollback()
            if not connection.invalidated:
                await connection.execute(text(f"SELECT pg_advisory_unlock({LOCK_KEY})"))
                await connection.commit()
        except Exception:
            await connection.invalidate()
        finally:
            await connection.close()
