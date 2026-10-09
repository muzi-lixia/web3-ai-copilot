"""版本化业务路由统一注册；URL 前缀由应用配置决定。"""

from fastapi import APIRouter

from app.api.v1 import auth, chat, market, risk, staking, wallet

# 入口统一增加 API 版本前缀，子路由保留自身领域前缀，避免重复或漏注册。
api_router = APIRouter()
for router in (auth.router, wallet.router, market.router, risk.router, staking.router, chat.router):
    api_router.include_router(router)
