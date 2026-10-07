"""质押仓位路由（功能点 4）。

    GET /staking/{address}/positions?chain_id=80094

与资产、风险接口同样的两道约束：只能查自己；阻塞调用不进这一层
（链上读取已在 `staking_service` 内部丢进线程池）。
"""

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_address
from app.api.schemas.staking import StakingSummary
from app.config.settings import settings
from app.services import staking_service
from app.shared.errors import ForbiddenError

router = APIRouter(prefix="/staking", tags=["staking"])


@router.get("/{address}/positions", response_model=StakingSummary, summary="质押仓位")
async def get_positions(
    address: str,
    chain_id: int | None = Query(default=None, description="不传则用默认链（见配置 default_chain_id）"),
    current_address: str = Depends(get_current_address),
) -> StakingSummary:
    """列出质押仓位：份额、折算后的底层数量、汇率、年化、累计收益与提款队列。

    `apy` / `earnings` 取自 Berachain 官方数据层（BeraHub 页面用的就是它），
    取不到时为 null —— 不是 0。数值全部来自链上或该接口，
    本层不做任何估算。
    """
    if address.lower() != current_address.lower():
        raise ForbiddenError("只能查询当前登录钱包的质押仓位")

    return await staking_service.get_summary(chain_id or settings.default_chain_id, address)
