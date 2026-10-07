"""钱包资产路由（功能点 1）。

    GET /wallet/{address}/assets?chain_id=80094

地址写在路径里（而不是从 token 里取），是为了给后续"看别人的地址"留位置：
服务端已经有能力查任意地址，限制只在这一行的权限判断上。
"""

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_address
from app.core.config import settings
from app.core.errors import ForbiddenError
from app.schemas.wallet import WalletAssets
from app.services import asset_service

router = APIRouter(prefix="/wallet", tags=["wallet"])


@router.get("/{address}/assets", response_model=WalletAssets, summary="查询钱包资产")
async def get_assets(
    address: str,
    chain_id: int | None = Query(default=None, description="不传则用默认链（见配置 default_chain_id）"),
    current_address: str = Depends(get_current_address),
) -> WalletAssets:
    """查询指定地址在某条链上的原生币与 ERC-20 余额，并附 USD 估值。

    两条约束：

    1. **只能查自己。** token 里已经带了地址，路径上的 address 必须与之一致，
       否则就是拿别人的凭证读第三个地址。想放开成"查询任意地址"时，
       只需改这一处判断。
    2. **阻塞调用不出现在这一层。** 链上读取是同步的，已经由 service 层丢进
       线程池；路由只做权限判断和参数归一。
    """
    if address.lower() != current_address.lower():
        raise ForbiddenError("只能查询当前登录钱包的资产")

    return await asset_service.get_wallet_assets(chain_id or settings.default_chain_id, address)
