"""基础服务入口。启动/关闭与 Agent 和 Ollama 完全独立，基础业务无需模型即可使用。"""

import asyncio
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health
from app.api.v1 import market, risk, staking, wallet
from app.core.config import get_settings
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import TraceMiddleware, setup_logging
from app.core.resources import BusinessResources
from app.foundation.api import router
from app.foundation.auth import AuthService
from app.foundation.results import ResultService
from app.infrastructure.database.session import Database


def create_app(services=None):
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
        )

    @asynccontextmanager
    async def lifespan(app):
        setup_logging("DEBUG" if settings.debug else "INFO")

        async def maintain():
            while True:
                await asyncio.sleep(60)
                try:
                    await services.auth.cleanup()
                except Exception:
                    from app.core.logging import get_logger

                    get_logger(__name__).exception("maintenance.failed")

        task = asyncio.create_task(maintain())
        try:
            yield
        finally:
            task.cancel()
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
    for r in (router, wallet.router, market.router, risk.router, staking.router):
        app.include_router(r, prefix=settings.api_prefix)
    app.include_router(health)
    return app


app = create_app()
