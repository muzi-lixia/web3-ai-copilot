"""资产风险分析路由（功能点 3）。

    GET /risk/{address}/report?chain_id=80094

与资产接口同样的两道约束：只能查自己；阻塞调用不进这一层
（链上读取已在 `asset_service` 内部丢进线程池）。
"""

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_address
from app.api.schemas.risk import RiskReport
from app.config.settings import settings
from app.services import risk_service
from app.shared.errors import ForbiddenError

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/{address}/report", response_model=RiskReport, summary="资产风险分析")
async def get_report(
    address: str,
    chain_id: int | None = Query(default=None, description="不传则用默认链（见配置 default_chain_id）"),
    current_address: str = Depends(get_current_address),
) -> RiskReport:
    """按持仓结构给出集中度、稳定币比、波动比与风险档。

    数值全部由代码算；`explanation`（自然语言解释）要等 LLM 模块接入，
    现在恒为 null，前端据此不渲染解释区块。
    """
    if address.lower() != current_address.lower():
        raise ForbiddenError("只能分析当前登录钱包的资产")

    return await risk_service.assess(chain_id or settings.default_chain_id, address)
