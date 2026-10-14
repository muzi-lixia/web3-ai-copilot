"""基础服务入口。启动/关闭与 Agent 和 Ollama 完全独立，基础业务无需模型即可使用。"""

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.http.errors import register_exception_handlers
from app.core.logging import TraceMiddleware, get_logger, service_name, setup_logging
from app.foundation.api.health import router as health
from app.foundation.api.v1 import asset_results, assets, auth, knowledge, market, networks, risk, staking
from app.foundation.config import get_foundation_settings
from app.foundation.container import build_services


def create_app(services=None):
    """组装基础服务应用。

    传入 services 时复用它（测试用）；否则自建数据库连接、资源与各服务实例。
    基础服务的启动/关闭与 Agent、Ollama 完全解耦，无模型也能独立提供业务。
    """
    settings = services.settings if services else get_foundation_settings()
    if services is None:
        services = build_services(settings)

    @asynccontextmanager
    async def lifespan(app):
        """启动时装配日志并拉起维护任务；关闭时取消维护并释放数据库连接。"""
        service_token = service_name.set("foundation")
        setup_logging()

        async def maintain():
            """周期性清理过期挑战、结果与限流桶；单次失败只记日志，不中断循环。"""
            while True:
                await asyncio.sleep(60)
                try:
                    await services.auth.cleanup()
                except Exception:
                    get_logger(__name__).exception("maintenance.failed")

        tasks = []
        try:
            await services.database.validate_embedding_dimensions(settings.knowledge_embedding_dimensions)
            tasks.append(asyncio.create_task(maintain()))
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            await services.database.close()
            service_name.reset(service_token)

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
    for r in (
        auth.router,
        asset_results.router,
        networks.router,
        assets.router,
        market.router,
        risk.router,
        staking.router,
        knowledge.router,
    ):
        app.include_router(r, prefix=settings.api_prefix)
    app.include_router(health)
    return app


app = create_app()
