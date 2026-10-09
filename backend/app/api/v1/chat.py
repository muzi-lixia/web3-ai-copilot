"""对话资源接口：读取无业务写入，幂等创建会话、创建轮次、取消资源和 SSE 订阅。"""

import asyncio
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.ai.conversation.schemas import (
    MessagePage,
    SessionResponse,
    SnapshotEvent,
    StreamErrorEvent,
    TurnAccepted,
    TurnRequest,
    TurnSnapshot,
)
from app.api.dependencies import ChatServiceDep, CurrentAddress
from app.api.responses import success
from app.common.schemas import ApiResponse
from app.core.exceptions import AppError
from app.core.logging import get_logger

router = APIRouter(prefix="/chat", tags=["chat"])
logger = get_logger(__name__)


def public_snapshot(snapshot):
    """过滤内部实际模型输入，只公开正文、状态和降级原因。"""
    result = {**snapshot}
    result["context_info"] = {
        k: v for k, v in result.get("context_info", {}).items() if k != "model_messages"
    }
    return result


@router.put(
    "/session",
    response_model=ApiResponse[SessionResponse],
    responses={201: {"model": ApiResponse[SessionResponse]}},
)
async def put_session(request: Request, response: Response, owner: CurrentAddress, runtime: ChatServiceDep):
    """幂等确保本人会话存在；首次创建 201，重复调用 200，Location 标识资源地址。"""
    current = await runtime.session(owner)
    response.status_code = 201 if current["created"] else 200
    location = str(request.url_for("get_session"))
    response.headers["Location"] = location
    return success(current)


@router.get("/session", response_model=ApiResponse[SessionResponse])
async def get_session(owner: CurrentAddress, runtime: ChatServiceDep):
    """读取本人会话，不存在返回 404；查询不创建新会话。"""
    return success(await runtime.read_session(owner))


@router.delete("/data", status_code=204)
async def delete_data(owner: CurrentAddress, runtime: ChatServiceDep):
    """删除本人全部聊天数据，重复删除仍为 204，无 JSON 正文；不吊销 JWT。"""
    await runtime.delete_all(owner)


@router.get("/session/messages", response_model=ApiResponse[MessagePage])
async def list_messages(
    owner: CurrentAddress,
    runtime: ChatServiceDep,
    before_seq: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=100),
):
    """按游标读取历史，没有会话返回空页；data.next_cursor 标识更早一页。"""
    page = await runtime.history(owner, before_seq, limit)
    return success(page)


@router.post("/session/turns", status_code=202, response_model=ApiResponse[TurnAccepted])
async def create_turn(
    payload: TurnRequest, request: Request, response: Response, owner: CurrentAddress, runtime: ChatServiceDep
):
    """创建异步生成尝试；message 创建新问题，retry_of 复用原问题和已保存上下文。"""
    result = (
        await runtime.retry(owner, payload.retry_of, payload.client_msg_id)
        if payload.retry_of
        else await runtime.submit_message(owner, payload.client_msg_id, payload.message)
    )
    response.headers["Location"] = str(request.url_for("get_turn", turn_id=result["turn_id"]))
    if (
        not result["created"]
        and (await runtime.snapshot(owner, UUID(result["turn_id"])))["status"] != "running"
    ):
        response.status_code = 200
    else:
        response.headers["Retry-After"] = "1"
    return success(result)


@router.get("/turns/{turn_id}", response_model=ApiResponse[TurnSnapshot])
async def get_turn(turn_id: UUID, owner: CurrentAddress, runtime: ChatServiceDep):
    """读取轮次表示，权限不符为 404；孤儿状态由后台恢复任务维护。"""
    return success(public_snapshot(await runtime.snapshot(owner, turn_id)))


@router.put("/turns/{turn_id}/cancellation", response_model=ApiResponse[TurnSnapshot])
async def put_cancellation(turn_id: UUID, owner: CurrentAddress, runtime: ChatServiceDep):
    """幂等请求停止生成，重复请求不创建新尝试；返回最终轮次表示。"""
    return success(public_snapshot(await runtime.cancel(owner, turn_id)))


@router.get("/turns/{turn_id}/events", response_class=StreamingResponse)
async def turn_events(turn_id: UUID, owner: CurrentAddress, runtime: ChatServiceDep):
    """订阅既有轮次，建连前鉴权，流内错误采用 error 事件；不创建或恢复业务数据。"""
    await runtime.snapshot(owner, turn_id)

    async def stream():
        """SSE 使用事件契约，不包装成普通 JSON 文档；断开只结束订阅。"""
        try:
            async for event in runtime.subscribe(owner, turn_id):
                if event["type"] == "ping":
                    yield ": ping\n\n"
                else:
                    encoded = SnapshotEvent.model_validate(public_snapshot(event)).model_dump_json()
                    yield f"event: {event['type']}\ndata: {encoded}\n\n"
        except asyncio.CancelledError:
            raise
        except AppError as exc:
            encoded = StreamErrorEvent(message=exc.message, code=exc.code).model_dump_json()
            yield f"event: error\ndata: {encoded}\n\n"
        except Exception:
            logger.exception("Snapshot subscription failed")
            encoded = StreamErrorEvent(message="连接异常，请重新连接").model_dump_json()
            yield f"event: error\ndata: {encoded}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
