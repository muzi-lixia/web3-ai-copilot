"""当前用户业务接口：查询目标只能来自有效登录凭证，禁止客户端填写地址。"""

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import BusinessResourcesDep, get_current_address
from app.api.responses import success
from app.common.schemas import ApiResponse
from app.modules.risk import service as risk_service
from app.modules.risk.schemas import RiskReport

router = APIRouter(prefix="/me", tags=["风险分析"])


@router.get("/risk-report", response_model=ApiResponse[RiskReport], summary="资产风险分析")
async def get_report(
    resources: BusinessResourcesDep,
    chain_id: int | None = Query(default=None, gt=0, description="不传则用默认链（见配置 default_chain_id）"),
    current_address: str = Depends(get_current_address),
) -> dict:
    """按持仓结构给出集中度、稳定币比、波动比与风险档。

    数值全部由代码计算，不调用模型。
    """
    return success(
        await risk_service.assess(
            chain_id or resources.settings.default_chain_id, current_address, resources=resources
        )
    )
