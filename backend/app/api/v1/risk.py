"""资产风险分析路由（功能点 3）。

    GET /wallets/{address}/risk-report?chain_id=80094

与资产接口同样的两道约束：只能查自己；阻塞调用不进这一层
（链上读取已在 `asset_service` 内部丢进线程池）。
"""

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_current_address
from app.api.responses import success
from app.common.schemas import Address, ApiResponse
from app.core.config import settings
from app.core.exceptions import ForbiddenError
from app.modules.risk import service as risk_service
from app.modules.risk.schemas import RiskReport

router = APIRouter(prefix="/wallets", tags=["risk"])


@router.get("/{address}/risk-report", response_model=ApiResponse[RiskReport], summary="资产风险分析")
async def get_report(
    address: Address,
    chain_id: int | None = Query(default=None, gt=0, description="不传则用默认链（见配置 default_chain_id）"),
    current_address: str = Depends(get_current_address),
) -> dict:
    """按持仓结构给出集中度、稳定币比、波动比与风险档。

    数值全部由代码计算，不调用模型。
    """
    if address.lower() != current_address.lower():
        raise ForbiddenError("只能分析当前登录钱包的资产")

    return success(await risk_service.assess(chain_id or settings.default_chain_id, address))
