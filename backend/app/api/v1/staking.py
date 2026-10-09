"""质押仓位路由（功能点 4）。

    GET /wallets/{address}/staking-positions?chain_id=80094

与资产、风险接口同样的两道约束：只能查自己；阻塞调用不进这一层
（链上读取已在 `staking_service` 内部丢进线程池）。
"""

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_current_address
from app.api.responses import success
from app.common.schemas import Address, ApiResponse
from app.core.config import settings
from app.core.exceptions import ForbiddenError
from app.modules.staking import service as staking_service
from app.modules.staking.schemas import StakingSummary

router = APIRouter(prefix="/wallets", tags=["staking"])


@router.get("/{address}/staking-positions", response_model=ApiResponse[StakingSummary], summary="质押仓位")
async def get_positions(
    address: Address,
    chain_id: int | None = Query(default=None, gt=0, description="不传则用默认链（见配置 default_chain_id）"),
    current_address: str = Depends(get_current_address),
) -> dict:
    """列出质押仓位：份额、折算后的底层数量、汇率、年化、累计收益与提款队列。

    `apy` / `earnings` 取自 Berachain 官方数据层（BeraHub 页面用的就是它），
    取不到时为 null —— 不是 0。数值全部来自链上或该接口，
    本层不做任何估算。
    """
    if address.lower() != current_address.lower():
        raise ForbiddenError("只能查询当前登录钱包的质押仓位")

    return success(await staking_service.get_summary(chain_id or settings.default_chain_id, address))
