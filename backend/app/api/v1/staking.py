"""当前用户业务接口：查询目标只能来自有效登录凭证，禁止客户端填写地址。"""

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import BusinessResourcesDep, get_current_address
from app.api.responses import success
from app.common.schemas import ApiResponse
from app.modules.staking import service as staking_service
from app.modules.staking.schemas import StakingSummary

router = APIRouter(prefix="/me", tags=["质押"])


@router.get("/staking-positions", response_model=ApiResponse[StakingSummary], summary="质押仓位")
async def get_positions(
    resources: BusinessResourcesDep,
    chain_id: int | None = Query(default=None, gt=0, description="不传则用默认链（见配置 default_chain_id）"),
    current_address: str = Depends(get_current_address),
) -> dict:
    """列出质押仓位：份额、折算后的底层数量、汇率、年化、累计收益与提款队列。

    `apy` / `earnings` 取自 Berachain 官方数据层（BeraHub 页面用的就是它），
    取不到时为 null —— 不是 0。数值全部来自链上或该接口，
    本层不做任何估算。
    """
    return success(
        await staking_service.get_summary(
            chain_id or resources.settings.default_chain_id, current_address, resources=resources
        )
    )
