"""Alembic 异步迁移入口。

应用连接使用独立服务账号，迁移连接使用 app_ddl，两者不能混用。
当前表结构由版本化 SQL 显式维护，不使用 ORM 自动生成迁移。
在线模式执行实际 DDL；离线模式输出 SQL。迁移自身是一条短任务，不复用应用连接池。
"""

import asyncio

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.core.config import settings


def migrate(connection):
    """在 Alembic 事务中执行迁移；执行冻结的版本化 SQL，不从运行代码推导表结构。"""
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    """使用专用迁移身份和无连接池引擎，结束后立即释放连接。"""
    engine = create_async_engine(settings.database_migration_url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(migrate)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=settings.database_migration_url, target_metadata=None, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
