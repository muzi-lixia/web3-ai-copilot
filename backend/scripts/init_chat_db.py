"""初始化本地数据库角色，然后执行 Alembic 迁移；不打印口令。

先运行 docker compose up -d postgres，再运行 uv run python scripts/init_chat_db.py。
生产通过 CHAT_DB_ADMIN_URL、CHAT_DDL_PASSWORD、CHAT_RW_PASSWORD 传入独立凭据。
角色只在不存在时创建，重复执行不会覆盖已有口令；应用不需要获得管理员连接权限。
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]


async def initialize():
    """由管理员创建缺失的角色与权限；未来 app_ddl 新建的表自动授予 app_rw 业务读写权限。"""
    url = os.getenv(
        "CHAT_DB_ADMIN_URL", "postgresql://copilot_admin:copilot_dev_admin@127.0.0.1:54329/web3copilot"
    )
    connection = await asyncpg.connect(url)
    try:
        for role, env, default in (
            ("app_ddl", "CHAT_DDL_PASSWORD", "copilot_dev_ddl"),
            ("app_rw", "CHAT_RW_PASSWORD", "copilot_dev_rw"),
            ("app_foundation", "FOUNDATION_DB_PASSWORD", "copilot_dev_foundation"),
            ("app_agent", "AGENT_DB_PASSWORD", "copilot_dev_agent"),
        ):
            if not await connection.fetchval("SELECT 1 FROM pg_roles WHERE rolname=$1", role):
                # 角色名来自固定清单；口令需转义 SQL 字符串引号，且不写入日志。
                password = os.getenv(env, default).replace("'", "''")
                await connection.execute(f"CREATE ROLE {role} LOGIN NOBYPASSRLS PASSWORD '{password}'")
        # 只有迁移角色获得 schema 创建权，运行角色只获得使用权和后续表的读写权。
        await connection.execute("GRANT USAGE, CREATE ON SCHEMA public TO app_ddl")
        await connection.execute("GRANT USAGE ON SCHEMA public TO app_rw, app_foundation, app_agent")
        await connection.execute("""ALTER DEFAULT PRIVILEGES FOR ROLE app_ddl IN SCHEMA public
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw""")
    finally:
        await connection.close()


if __name__ == "__main__":
    # 先创建角色和默认权限，再迁移建表；应用账号不需要 DDL 权限。
    asyncio.run(initialize())
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        check=True,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    sys.path.insert(0, str(ROOT))
    # 框架表由迁移账号创建；应用运行账号仅获得读写权，不执行 saver.setup。
    from langgraph.checkpoint.postgres import PostgresSaver
    from sqlalchemy.engine import make_url

    from app.core.config import settings

    dsn = (
        make_url(settings.database_migration_url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.setup()

    async def grant_service_roles():
        connection = await asyncpg.connect(
            os.getenv(
                "CHAT_DB_ADMIN_URL",
                "postgresql://copilot_admin:copilot_dev_admin@127.0.0.1:54329/web3copilot",
            )
        )
        try:
            await connection.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON "
                "service_users, auth_challenges, login_sessions, refresh_credentials, request_limits, "
                "security_audit, asset_results TO app_foundation"
            )
            await connection.execute(
                "GRANT USAGE, SELECT ON SEQUENCE security_audit_id_seq TO app_foundation"
            )
            await connection.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON "
                "agent_conversations, agent_turns, agent_result_refs, checkpoints, checkpoint_blobs, "
                "checkpoint_writes, checkpoint_migrations TO app_agent"
            )
            await connection.execute("GRANT EXECUTE ON FUNCTION agent_recover_turns() TO app_agent")
        finally:
            await connection.close()

    asyncio.run(grant_service_roles())
    print("Copilot database initialized.")
