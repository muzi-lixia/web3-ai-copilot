"""FastAPI 入口。

每个模块的 router 写在 api/<模块>.py，统一挂载在 settings.api_prefix 下。
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, market, risk, staking, wallet
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import setup_logging

setup_logging("DEBUG" if settings.debug else "INFO")

app = FastAPI(
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
