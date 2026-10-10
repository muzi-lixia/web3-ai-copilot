"""对话 HTTP 入口：只收当前消息和幂等键，不接受历史、地址或身份声明。"""

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.privacy import SafeInput, normalize_query
from app.api.responses import success
from app.core.exceptions import AppError, UnauthorizedError

router = APIRouter(prefix="/chat", tags=["Agent 对话"])
bearer = HTTPBearer(auto_error=False)


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str | None = Field(default=None, min_length=1, max_length=2000)
    client_msg_id: UUID
    retry_of: UUID | None = None

    @model_validator(mode="after")
    def exclusive(self):
        if (self.message is None) == (self.retry_of is None):
            raise ValueError("message 与 retry_of 必须且只能提供一个")
        if self.message is not None and not self.message.strip():
            raise ValueError("message 不能只包含空白字符")
        return self


async def current(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
):
    if not credentials:
        raise UnauthorizedError("缺少登录凭证")
    identity = await request.app.state.services.client.identity(credentials.credentials)
    return {**identity, "token": credentials.credentials}


Current = Annotated[dict, Depends(current)]


@router.put("/session", summary="打开本人固定会话")
async def open_session(request: Request, response: Response, user: Current):
    result = await request.app.state.services.repository.conversation(user["user_id"])
    if result.get("expired_thread_id"):
        await request.app.state.services.chat.checkpointer.adelete_thread(result.pop("expired_thread_id"))
    response.status_code = 201 if result["created"] else 200
    return success(result)


@router.get("/session/messages", summary="读取本人消息历史")
async def history(
    request: Request,
    user: Current,
    before_seq: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=100),
):
    return success(await request.app.state.services.repository.history(user["user_id"], before_seq, limit))


@router.post("/session/turns", status_code=202, summary="提交问题或重试轮次")
async def submit(body: TurnRequest, request: Request, user: Current):
    services = request.app.state.services
    raw = body.message
    display_text = raw
    if body.retry_of:
        previous = await services.repository.read(user["user_id"], str(body.retry_of))
        raw = previous["query"]
        display_text = previous.get("display_text")
        if display_text is None:
            display_text = raw
        # 已经安全化的问题不能再次被解析器误判。
        safe = SafeInput(raw)
    else:
        safe = normalize_query(raw)
        import re

        from app.infrastructure.blockchain.chains import CHAINS

        addresses = re.findall(r"0x[a-fA-F0-9]{40}", raw)
        if len(addresses) == 1 and any(w in raw for w in ("合约", "代币地址")):
            chains = [
                c
                for c in CHAINS
                if c.key in raw.lower()
                or c.name.lower() in raw.lower()
                or re.search(rf"(?<![0-9]){c.chain_id}(?![0-9])", raw)
            ]
            if len(chains) == 1:
                resolved = await services.client.request(
                    "POST",
                    "/tokens/resolutions",
                    user["token"],
                    json={"chain_id": chains[0].chain_id, "contract": addresses[0]},
                )
                # 已验证的公开代币合约可进入模型，原有问句和语序不再替换成模板。
                safe = normalize_query(raw, public_contracts=(resolved["contract"],))
            else:
                safe = SafeInput("用户请求代币合约查询但网络缺失。", "请明确代币合约所在的网络。")
    # 展示原文不进入模型；重试同时复用原轮次的展示文本和安全输入。
    return success(
        await services.chat.submit(
            user["user_id"], user["token"], str(body.client_msg_id), safe, display_text=display_text
        )
    )


@router.get("/turns/{turn_id}", summary="读取轮次状态快照")
async def snapshot(turn_id: UUID, request: Request, user: Current):
    return success(await request.app.state.services.chat.snapshot(user["user_id"], str(turn_id)))


@router.put("/turns/{turn_id}/cancellation", summary="停止生成")
async def cancel(turn_id: UUID, request: Request, user: Current):
    return success(await request.app.state.services.chat.cancel(user["user_id"], str(turn_id)))


@router.delete("/data", status_code=204, summary="清空本人对话及关联结果")
async def clear(request: Request, user: Current):
    await request.app.state.services.chat.clear(user["user_id"], user["token"])
    return Response(status_code=204)


@router.get("/turns/{turn_id}/events", summary="订阅 SSE 对话快照")
async def events(turn_id: UUID, request: Request, user: Current):
    chat = request.app.state.services.chat
    await chat.snapshot(user["user_id"], str(turn_id))

    async def stream():
        try:
            async for event in chat.events(user["user_id"], str(turn_id), user["token"]):
                if await request.is_disconnected():
                    return
                if event["type"] == "ping":
                    yield ": ping\n\n"
                else:
                    yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        except AppError as exc:
            yield (
                "event: error\ndata: "
                + json.dumps({"type": "error", "message": exc.message, "code": exc.code})
                + "\n\n"
            )

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    accept_external: bool = False


@router.put("/session/model", summary="切换本人对话模型")
async def model(body: ModelSelection, request: Request, user: Current):
    """普通用户仅改变自己的对话模型，不能修改全局配置或提供凭据。"""
    from app.agent.models import select_settings
    from app.core.exceptions import ForbiddenError

    settings = request.app.state.services.settings
    chosen = select_settings(settings, body.provider)
    if chosen.model_provider != "ollama" and not body.accept_external:
        raise ForbiddenError("使用在线模型需要明确确认安全对话元数据会外发")
    repository = request.app.state.services.repository
    conversation = await repository.conversation(user["user_id"])
    await repository.set_model(user["user_id"], conversation["id"], chosen.model_provider)
    return success(
        {
            "provider": chosen.model_provider,
            "model": chosen.model_name,
            "context_window": chosen.model_context_window or 16384,
        }
    )


@router.get("/session/model", summary="读取模型配置")
async def selected_model(request: Request, user: Current):
    """只公开当前用户的模型选择与可用目录，不创建会话，也不返回部署凭据。"""
    from sqlalchemy import text

    from app.agent.models import select_settings

    services = request.app.state.services
    async with services.repository.db.transaction(user["user_id"]) as session:
        provider = await session.scalar(
            text("SELECT model_provider FROM agent_conversations WHERE user_id=:u AND expires_at>now()"),
            {"u": user["user_id"]},
        )
    available = []
    selected = None
    for name in ("ollama", "deepseek", "qwen"):
        try:
            chosen = select_settings(services.settings, name)
            entry = {
                "provider": name,
                "model": chosen.model_name,
                "enabled": True,
                "external": name != "ollama",
                "context_window": chosen.model_context_window or 16384,
            }
        except AppError:
            entry = {"provider": name, "model": None, "enabled": False, "external": name != "ollama"}
        available.append(entry)
        if name == (provider or services.settings.model_provider):
            selected = entry
    return success(
        {**selected, "available": available, "context_ttl_hours": services.settings.conversation_ttl_hours}
    )
