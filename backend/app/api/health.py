"""存活与就绪探针，独立于业务 API 版本。"""

from fastapi import APIRouter, Request

from app.api.responses import success
from app.common.schemas import ApiResponse
from app.core.exceptions import ChatUnavailableError

router = APIRouter(tags=["health"])


@router.get("/health", response_model=ApiResponse[dict])
async def health(request: Request) -> dict:
    """存活探针：说明应用能够处理请求，并返回默认链标识。

    不探测数据库、Ollama 或 RPC；运行锁是否有效由 /ready 单独判断。
    """
    return success({"status": "ok", "chain_id": request.app.state.services.settings.default_chain_id})


@router.get("/ready", response_model=ApiResponse[dict])
async def ready(request: Request) -> dict:
    """确认运行时已启动并持有原租约，上游模型故障由业务请求独立报告。"""
    service = request.app.state.services.chat
    if service.stopping:
        raise ChatUnavailableError("聊天服务正在停止")
    await service.verify_lease()
    return success({"status": "ready"})
