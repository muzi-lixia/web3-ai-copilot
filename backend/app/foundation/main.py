"""基础服务入口。启动/关闭与 Agent 和 Ollama 完全独立，基础业务无需模型即可使用。"""

import asyncio
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health
from app.api.v1 import knowledge, market, risk, staking, wallet
from app.core.config import get_settings
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import TraceMiddleware, get_logger, setup_logging
from app.core.resources import BusinessResources
from app.foundation.api import router
from app.foundation.auth import AuthService
from app.foundation.results import ResultService
from app.infrastructure.database.session import Database
from app.modules.knowledge.service import KnowledgeService

# 队列空转的间隔：上传到开始处理最多晚这么久。
POLL_SECONDS = 1.0


def create_app(services=None):
    """组装基础服务应用。

    传入 services 时复用它（测试用）；否则自建数据库连接、资源与各服务实例。
    基础服务的启动/关闭与 Agent、Ollama 完全解耦，无模型也能独立提供业务。
    """
    settings = services.settings if services else get_settings()
    if services is None:
        db = Database(settings.foundation_database_url or settings.database_url)
        resources = BusinessResources(settings)
        services = SimpleNamespace(
            settings=settings,
            database=db,
            resources=resources,
            auth=AuthService(db, settings),
            results=ResultService(db, resources),
            knowledge=KnowledgeService(db, settings),
        )

    @asynccontextmanager
    async def lifespan(app):
        """启动时装配日志并拉起维护任务；关闭时取消维护并释放数据库连接。"""
        setup_logging()
        # 上次中断时留在「处理中」的文档退回待处理，交给下面的循环重新跑。
        await services.knowledge.recover()

        async def maintain():
            """周期性清理过期挑战、结果与限流桶；单次失败只记日志，不中断循环。"""
            while True:
                await asyncio.sleep(60)
                try:
                    await services.auth.cleanup()
                except Exception:
                    get_logger(__name__).exception("maintenance.failed")

        async def consume():
            """轮询待处理文档跑离线链路；单篇失败已记在该文档上，这里只防循环中断。"""
            while True:
                try:
                    if not await services.knowledge.run_pending():
                        await asyncio.sleep(POLL_SECONDS)
                except Exception:
                    get_logger(__name__).exception("knowledge.consume_failed")
                    await asyncio.sleep(POLL_SECONDS)

        tasks = [asyncio.create_task(maintain()), asyncio.create_task(consume())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            await services.database.close()

    app = FastAPI(title="Web3 Foundation", lifespan=lifespan)
    app.state.services = services
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Trace-Id", "Location"],
    )
    app.add_middleware(TraceMiddleware, service="foundation")
    register_exception_handlers(app)
    for r in (router, wallet.router, market.router, risk.router, staking.router, knowledge.router):
        app.include_router(r, prefix=settings.api_prefix)
    app.include_router(health)
    return app


app = create_app()
