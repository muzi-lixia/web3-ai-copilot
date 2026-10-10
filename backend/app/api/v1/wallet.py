"""当前用户业务接口：查询目标只能来自有效登录凭证，禁止客户端填写地址。"""

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import BusinessResourcesDep, get_current_address
from app.api.responses import success
from app.common.schemas import ApiResponse
from app.modules.asset import service as asset_service
from app.modules.asset.schemas import WalletAssets

router = APIRouter(prefix="/me", tags=["资产查询"])


@router.get("/assets", response_model=ApiResponse[WalletAssets], summary="查询钱包资产")
async def get_assets(
    resources: BusinessResourcesDep,
    chain_id: int | None = Query(default=None, gt=0, description="不传则用默认链（见配置 default_chain_id）"),
    current_address: str = Depends(get_current_address),
) -> dict:
    """查询当前登录钱包的原生币和已收录 ERC20 余额，并附 USD 估值。

    钱包身份由鉴权依赖注入，不接收外部地址。业务服务负责链上读取和估值；
    本路由只接收网络选择并返回统一响应，不包含模型或工具逻辑。
    """
    return success(
        await asset_service.get_wallet_assets(
            chain_id or resources.settings.default_chain_id, current_address, resources=resources
        )
    )
