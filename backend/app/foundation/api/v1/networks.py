"""基础服务 HTTP 接口；业务逻辑由 modules 提供。"""

from fastapi import APIRouter, Request

from app.core.http.responses import success
from app.foundation.api.dependencies import bearer
from app.infrastructure.blockchain.chains import CHAINS
from app.modules.asset.tokens import tokens_for

router = APIRouter()


@router.get(
    "/chains",
    tags=["公共能力"],
    summary="读取支持的网络与代币目录",
    openapi_extra={"security": [], "x-auth-description": "匿名可访问；提供凭证时仍校验"},
)
async def chains(request: Request):
    """列出支持的网络与各自代币目录。匿名可访问，但带凭证时仍验证并改用更宽松的用户限流。"""
    auth = request.app.state.services.auth
    credentials = await bearer(request)
    # 匿名也放行，但按来源 IP 用更严的匿名配额；带凭证则按用户使用业务配额。
    if credentials:
        current = await auth.authenticate(credentials.credentials)
        key, limit = "chains:user:" + current.user_id, auth.settings.request_limit_per_minute
    else:
        key = "chains:anonymous:" + (request.client.host if request.client else "unknown")
        limit = auth.settings.anonymous_limit_per_minute
    await auth.limit(key, limit)
    return success(
        [
            {
                "chain_id": c.chain_id,
                "name": c.name,
                "key": c.key,
                "symbols": [t.symbol for t in tokens_for(c.chain_id)],
            }
            for c in CHAINS
        ]
    )
