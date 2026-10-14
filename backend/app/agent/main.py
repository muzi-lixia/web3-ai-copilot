"""Agent 单独部署入口。只连接自己的对话数据库、基础 HTTP 服务和模型。"""

import asyncio
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.agent.api import router
from app.agent.config import get_agent_settings
from app.agent.repository import ConversationRepository
from app.agent.service import ChatService
from app.agent.tracing import configure_tracing
from app.core.http.errors import register_exception_handlers
from app.core.logging import TraceMiddleware, get_logger, service_name, setup_logging
from app.infrastructure.database.dsn import postgres_dsn
from app.infrastructure.database.runtime_lock import RuntimeLock
from app.infrastructure.database.session import Database
from app.infrastructure.foundation_client import FoundationClient


def enable_tracing(settings):
    """把追踪配置写进进程环境：langsmith 只读环境变量，不经过这里的 Settings。

    必须在任何 LangChain 调用之前执行 —— langsmith 读取环境变量带 lru_cache，
    一旦先读过一次空值，之后再设也不会生效，且不会有任何报错。
    留空 key 即不打开开关，本地开发与测试不产生任何外发。
    """
    hidden = "false" if settings.langsmith_capture_content else "true"
    os.environ["LANGSMITH_HIDE_INPUTS"] = hidden
    os.environ["LANGSMITH_HIDE_OUTPUTS"] = hidden
    key = settings.langsmith_api_key.get_secret_value()
    if not key:
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        return
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project


def create_app(settings=None):
    """构造 Agent 服务的 FastAPI 实例。

    Agent 是独立部署单元：只连自己的对话库、基础 HTTP 服务和模型，
    因此依赖全部在这里显式装配并挂在 app.state 上，不使用全局单例。
    """
    settings = settings or get_agent_settings()

    @asynccontextmanager
    async def lifespan(app):
        """启动时接租约并收尾遗留轮次；关闭时先停任务、再释放客户端与连接池。"""
        service_token = service_name.set("agent")
        setup_logging()
        enable_tracing(settings)
        tracing_client = configure_tracing(settings)
        db = Database(settings.agent_database_url)
        client = FoundationClient(settings)
        client.default_chain_id = settings.default_chain_id
        try:
            await db.validate_agent_schema()
            # setup 由迁移脚本用 DDL 身份执行；运行账号不能在启动时创建框架表。
            async with AsyncPostgresSaver.from_conn_string(
                postgres_dsn(settings.agent_database_url)
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
            try:
                if tracing_client:
                    await asyncio.wait_for(asyncio.to_thread(tracing_client.flush), timeout=5)
            except Exception:
                get_logger(__name__).exception("tracing.flush_failed")
            finally:
                if tracing_client:
                    tracing_client.close()
                service_name.reset(service_token)

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

    @app.get("/ready")
    async def ready():
        await app.state.services.chat.lease.verify()
        return {"code": 0, "msg": "success", "data": {"service": "agent", "status": "ready"}}

    @app.get("/health")
    async def health():
        """存活探针：只说明进程在监听，不探测下游依赖，避免依赖抖动引发反复重启。"""
        return {"code": 0, "msg": "success", "data": {"service": "agent"}}

    return app


app = create_app()
