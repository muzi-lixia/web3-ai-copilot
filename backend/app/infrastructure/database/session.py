"""数据库资源与用户事务。引擎由应用容器创建和关闭，不在导入模块时建立全局资源。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class Database:
    """应用独占的异步数据库连接池与会话工厂。

    不保存当前用户身份；身份仅在每次 transaction 内设置，避免连接复用造成串用户。
    """

    def __init__(self, url: str):
        """创建惰性连接引擎和会话工厂，连接取出时检测可用性。

        expire_on_commit=False 允许提交后读取已加载字段；不表示可以跨事务
        复用 ORM 会话。实际连接在第一次数据库操作时才建立。
        """
        # 创建异步数据库引擎：pool_pre_ping 在取连接时检测存活。
        # pool_size 为常驻连接数，max_overflow 允许额外 5 个临时连接，最大并发为 10。
        self.engine = create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
        # 创建会话工厂，连接池为空时新建连接。expire_on_commit=False 允许提交后读已加载字段；
        # 不表示可以跨事务复用 ORM 会话。
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def transaction(self, user_id: str) -> AsyncIterator[AsyncSession]:
        """每次操作独立会话；用户标识只在事务内生效，连接归池后不会串用户。"""
        async with self.sessions() as session:
            async with session.begin():
                # true 表示设置只持续到当前事务结束；连接归池后不携带上个用户身份。
                await session.execute(
                    text("SELECT set_config('app.current_user', :addr, true)"), {"addr": user_id.lower()}
                )
                # 正常离开 begin 自动提交，异常自动回滚；调用方不要在此处执行长模型推理。
                yield session

    async def close(self) -> None:
        """销毁连接池，归还所有空闲连接；调用方应先等待正在使用数据库的任务结束。"""
        await self.engine.dispose()
