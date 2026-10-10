"""Agent 单独部署入口。只连接自己的对话数据库、基础 HTTP 服务和模型。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.engine import make_url

from app.agent.api import router
from app.agent.repository import ConversationRepository
from app.agent.service import ChatService
from app.core.config import get_settings
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import TraceMiddleware, setup_logging
from app.infrastructure.database.runtime_lock import RuntimeLock
from app.infrastructure.database.session import Database
from app.infrastructure.foundation_client import FoundationClient


def postgres_dsn(url):
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def create_app(settings=None):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        setup_logging("DEBUG" if settings.debug else "INFO")
        db = Database(settings.agent_database_url or settings.database_url)
        client = FoundationClient(settings)
        client.default_chain_id = settings.default_chain_id
        try:
            # setup 由迁移脚本用 DDL 身份执行；运行账号不能在启动时创建框架表。
            async with AsyncPostgresSaver.from_conn_string(
                postgres_dsn(settings.agent_database_url or settings.database_url)
            ) as saver:
                repository = ConversationRepository(db, settings)
                chat = ChatService(repository, client, settings, RuntimeLock(db.engine), saver)
                app.state.services = SimpleNamespace(
                    settings=settings, repository=repository, chat=chat, client=client
                )
                await chat.start()
                try:
                    yield
                finally:
                    await chat.stop()
        finally:
            await client.close()
            await db.close()

    app = FastAPI(title="Web3 Agent", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Trace-Id"],
    )
    app.add_middleware(TraceMiddleware, service="agent")
    register_exception_handlers(app)
    app.include_router(router, prefix=settings.api_prefix)

    @app.get("/health")
    async def health():
        return {"code": 0, "msg": "success", "data": {"service": "agent"}}

    return app


app = create_app()
