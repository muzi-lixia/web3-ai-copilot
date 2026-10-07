"""FastAPI 入口。

每个模块的 router 写在 api/routes/<模块>.py，统一挂载在 settings.api_prefix 下。
"""

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.exception_handlers import register_exception_handlers
from app.api.routes import auth, market, risk, staking, wallet
from app.config.settings import settings
from app.infra.logging import setup_logging
from app.services.auth_service import cleanup_auth_state

setup_logging("DEBUG" if settings.debug else "INFO")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async def cleanup():
        while True:
            cleanup_auth_state()
            await asyncio.sleep(60)

    task = asyncio.create_task(cleanup())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    lifespan=lifespan,
    title=settings.app_name,
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
    openapi_url=f"{settings.api_prefix}/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

app.include_router(auth.router, prefix=settings.api_prefix)
app.include_router(wallet.router, prefix=settings.api_prefix)
app.include_router(market.router, prefix=settings.api_prefix)
app.include_router(risk.router, prefix=settings.api_prefix)
app.include_router(staking.router, prefix=settings.api_prefix)


@app.get("/health", tags=["meta"], summary="健康检查")
async def health() -> dict[str, str | int]:
    return {"status": "ok", "chain_id": settings.default_chain_id}
