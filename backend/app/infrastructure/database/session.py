"""数据库资源与用户事务。引擎由应用容器创建和关闭，不在导入模块时建立全局资源。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.infrastructure.database.observability import ObservedSession, instrument_engine


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
        instrument_engine(self.engine)
        self.sessions = async_sessionmaker(self.engine, class_=ObservedSession, expire_on_commit=False)

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

    async def validate_agent_schema(self) -> None:
        """运行账号只检查自身契约，不读取迁移账号的 alembic_version 或执行 DDL。"""
        async with self.sessions() as session:
            ready = await session.scalar(text("""
                SELECT to_regclass('agent_checkpoint_deletions') IS NOT NULL
                  AND to_regprocedure('agent_runtime_status()') IS NOT NULL
                  AND to_regprocedure('agent_observability_status()') IS NOT NULL
                  AND EXISTS (SELECT 1 FROM pg_attribute
                      WHERE attrelid=to_regclass('agent_turns') AND attname='run_id' AND NOT attisdropped)
                  AND EXISTS (SELECT 1 FROM pg_attribute
                      WHERE attrelid=to_regclass('agent_turns') AND attname='timings' AND NOT attisdropped)
            """))
        if not ready:
            raise RuntimeError(
                "Agent 数据库结构未就绪，需要迁移至 0015_observability。"
                "请在 backend 目录使用迁移账号执行 uv run alembic upgrade head，然后重启 Agent；"
                "DATABASE_MIGRATION_URL 与 AGENT_DATABASE_URL 须指向同一数据库。"
            )

    async def validate_embedding_dimensions(self, dimensions: int) -> None:
        """启动时核对向量列维度；模型配置不能代替数据库结构迁移。"""
        async with self.sessions() as session:
            column_dimensions = await session.scalar(text("""
                SELECT a.atttypmod
                FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid
                WHERE a.attrelid = 'chunks'::regclass AND a.attname = 'embedding'
                  AND NOT a.attisdropped AND t.typname = 'vector'
            """))
        if column_dimensions != dimensions:
            raise RuntimeError(
                f"chunks.embedding 向量维度为 {column_dimensions}，配置要求 {dimensions}；"
                "请先完成向量列迁移和文档重建"
            )

    async def close(self) -> None:
        """销毁连接池，归还所有空闲连接；调用方应先等待正在使用数据库的任务结束。"""
        await self.engine.dispose()
