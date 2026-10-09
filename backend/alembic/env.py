"""Alembic 异步迁移入口。

应用连接使用 app_rw，迁移连接使用 app_ddl，两者不能混用。
在线模式执行实际 DDL；离线模式输出 SQL。迁移自身是一条短任务，不复用应用连接池。
"""

import asyncio

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.ai.conversation import models  # noqa: F401 -- 注册领域实体
from app.core.config import settings
from app.infrastructure.database.base import Base


def migrate(connection):
    """在 Alembic 事务中执行迁移；target_metadata 只用于模型对照，不重写已有迁移 SQL。"""
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    """使用专用迁移身份和无连接池引擎，结束后立即释放连接。"""
    engine = create_async_engine(settings.database_migration_url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(migrate)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=settings.database_migration_url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
