"""Token 行情路由（功能点 2）。

    GET /markets/quotes?chain_id=80094&symbols=BERA,WETH

一个接口覆盖"页面展示"和"给 Agent 供数"两种用法：后者直接调 services 层，
不经过这里 —— 但两者用的是同一份缓存，所以 Agent 拿到的是同一批价格。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials

from app.api.dependencies import BusinessResourcesDep, bearer
from app.api.responses import success
from app.common.schemas import ApiResponse
from app.modules.market import service as market_service
from app.modules.market.schemas import TokenMarketList

router = APIRouter(prefix="/markets", tags=["行情"])


@router.get(
    "/quotes",
    response_model=ApiResponse[TokenMarketList],
    summary="查询 Token 行情",
    openapi_extra={
        "security": [{}, {"WalletBearer": []}],
        "x-auth-description": "匿名可访问；提供凭证时仍校验",
    },
)
async def get_quotes(
    resources: BusinessResourcesDep,
    chain_id: int | None = Query(default=None, gt=0, description="不传则用默认链（见配置 default_chain_id）"),
    symbols: str | None = Query(
        default=None,
        description="逗号分隔的 symbol，如 BERA,WETH；不传则返回该链候选清单的全部",
    ),
    request: Request = None,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None,
) -> dict:
    """批量取价，一次请求覆盖多个 token。

    行情是显式匿名白名单。提供凭证时仍验证，非法凭证不能退回匿名路径。

    `missing` 不是错误，是**事实** —— 清单里像 BVT（无交易池）、BGT（不可转让）
    这样的币在任何免费源上都取不到价，它们会被原样列出来，
    而不是被当成价格为 0 或被悄悄丢掉。
    """
    auth = request.app.state.services.auth
    if credentials:
        current = await auth.authenticate(credentials.credentials)
        key, maximum = "market:user:" + current.user_id, auth.settings.request_limit_per_minute
    else:
        key = "market:anonymous:" + (request.client.host if request.client else "unknown")
        maximum = auth.settings.anonymous_limit_per_minute
    await auth.limit(key, maximum)
    requested = [part for part in (symbols or "").split(",") if part.strip()]
    return success(
        await market_service.get_quotes(
            chain_id or resources.settings.default_chain_id, requested or None, resources=resources
        )
    )
