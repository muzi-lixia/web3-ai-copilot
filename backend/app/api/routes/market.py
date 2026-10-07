"""Token 行情路由（功能点 2）。

    GET /market/quotes?chain_id=80094&symbols=BERA,WETH

一个接口覆盖"页面展示"和"给 Agent 供数"两种用法：后者直接调 services 层，
不经过这里 —— 但两者用的是同一份缓存，所以 Agent 拿到的是同一批价格。
"""

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_address
from app.api.schemas.market import TokenMarketList
from app.config.settings import settings
from app.services import market_service

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/quotes", response_model=TokenMarketList, summary="查询 Token 行情")
async def get_quotes(
    chain_id: int | None = Query(
        default=None, description="不传则用默认链（见配置 default_chain_id）"
    ),
    symbols: str | None = Query(
        default=None,
        description="逗号分隔的 symbol，如 BERA,WETH；不传则返回该链候选清单的全部",
    ),
    _: str = Depends(get_current_address),
) -> TokenMarketList:
    """批量取价，一次请求覆盖多个 token。

    行情本身是公开数据，这里仍然要求登录：全站其余接口都要凭证，留一个匿名口子
    只会让"到底哪些接口不设防"变成一个需要逐个确认的问题。

    `missing` 不是错误，是**事实** —— 清单里像 BVT（无交易池）、BGT（不可转让）
    这样的币在任何免费源上都取不到价，它们会被原样列出来，
    而不是被当成价格为 0 或被悄悄丢掉。
    """
    requested = [part for part in (symbols or "").split(",") if part.strip()]
    return await market_service.get_quotes(
        chain_id or settings.default_chain_id,
        requested or None,
    )
