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
    """提交一轮的请求体：新问题与重试互斥，且必须带幂等键。

    client_msg_id 是幂等的唯一依据；正文与重试标识只能有一个，
    避免出现「既像新问题又像重试」的歧义请求。
    """

    model_config = ConfigDict(extra="forbid")
    message: str | None = Field(default=None, min_length=1, max_length=2000)
    client_msg_id: UUID
    retry_of: UUID | None = None

    @model_validator(mode="after")
    def exclusive(self):
        """强制 message 与 retry_of 恰好提供一个，并拒绝纯空白问题。"""
        if (self.message is None) == (self.retry_of is None):
            raise ValueError("message 与 retry_of 必须且只能提供一个")
        if self.message is not None and not self.message.strip():
            raise ValueError("message 不能只包含空白字符")
        return self


async def current(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
):
    """把 Bearer 凭证换成服务端认定的用户身份，并带上 token 供下游调用。

    身份只来自凭证解析，不接受请求体或查询参数里的任何身份声明。
    """
    if not credentials:
        raise UnauthorizedError("缺少登录凭证")
    identity = await request.app.state.services.client.identity(credentials.credentials)
    return {**identity, "token": credentials.credentials}


Current = Annotated[dict, Depends(current)]


@router.put("/session", summary="打开本人固定会话")
async def open_session(request: Request, response: Response, user: Current):
    """幂等地打开本人会话；首次创建返回 201，已存在返回 200。"""
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
    """向前翻页读取消息历史；before_seq 是上一页返回的游标。"""
    return success(await request.app.state.services.repository.history(user["user_id"], before_seq, limit))


@router.post("/session/turns", status_code=202, summary="提交问题或重试轮次")
async def submit(body: TurnRequest, request: Request, user: Current):
    """受理一问一答，202 立即返回轮次 id，生成在后台进行。

    这里同时做三件事：把原文脱敏为模型安全输入、单独留存用于展示的原文、
    在代币合约这类必须有网络的场景下先解析再放行。展示原文不进模型。
    """
    services = request.app.state.services
    raw = body.message
    display_text = raw
    if body.retry_of:
        # 重试不重新脱敏：复用原轮次已经安全化的输入，安全文本再解析一次会被二次误判。
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
        # 只有「恰好一个合约地址 + 提到合约/代币地址」才进解析流程；其余按普通问题处理。
        if len(addresses) == 1 and any(w in raw for w in ("合约", "代币地址")):
            chains = [
                c
                for c in CHAINS
                if c.key in raw.lower()
                or c.name.lower() in raw.lower()
                or re.search(rf"(?<![0-9]){c.chain_id}(?![0-9])", raw)
            ]
            # 必须能唯一定位网络才继续：用 key / 名称 / chain_id 三种写法匹配。
            if len(chains) == 1:
                # 先让基础服务校验该合约确实是公开可读的代币，再放行。
                resolved = await services.client.request(
                    "POST",
                    "/tokens/resolutions",
                    user["token"],
                    json={"chain_id": chains[0].chain_id, "contract": addresses[0]},
                )
                # 已验证的公开代币合约可进入模型，原有问句和语序不再替换成模板。
                safe = normalize_query(raw, public_contracts=(resolved["contract"],))
            else:
                # 网络不唯一或缺网络：不进模型，走 clarification 通道直接反问。
                safe = SafeInput("用户请求代币合约查询但网络缺失。", "请明确代币合约所在的网络。")
    # 展示原文不进入模型；重试同时复用原轮次的展示文本和安全输入。
    return success(
        await services.chat.submit(
            user["user_id"], user["token"], str(body.client_msg_id), safe, display_text=display_text
        )
    )


@router.get("/turns/{turn_id}", summary="读取轮次状态快照")
async def snapshot(turn_id: UUID, request: Request, user: Current):
    """读取单轮状态；订阅失败或页面恢复时用它对齐当前进度。"""
    return success(await request.app.state.services.chat.snapshot(user["user_id"], str(turn_id)))


@router.put("/turns/{turn_id}/cancellation", summary="停止生成")
async def cancel(turn_id: UUID, request: Request, user: Current):
    """停止生成。取消是独立动作：断开订阅不等于取消，只有这里会真正停任务。"""
    return success(await request.app.state.services.chat.cancel(user["user_id"], str(turn_id)))


@router.delete("/data", status_code=204, summary="清空本人对话及关联结果")
async def clear(request: Request, user: Current):
    """清空本人对话数据。只影响当前用户，关联的工具结果一并清除，不可恢复。"""
    await request.app.state.services.chat.clear(user["user_id"], user["token"])
    return Response(status_code=204)


@router.get("/turns/{turn_id}/events", summary="订阅 SSE 对话快照")
async def events(turn_id: UUID, request: Request, user: Current):
    """订阅某轮次的 SSE 快照流。

    先做一次归属校验（越权直接 404），返回后由生成器持续推送；断开连接只结束订阅，
    不会取消正在进行的生成 —— 取消必须走独立的 cancellation 接口。
    """
    chat = request.app.state.services.chat
    await chat.snapshot(user["user_id"], str(turn_id))

    async def stream():
        """把业务事件翻译成 SSE 帧；ping 用心跳注释帧，其余按 type 命名事件。"""
        try:
            async for event in chat.events(user["user_id"], str(turn_id), user["token"]):
                # 客户端已断开就结束生成器；注意这只停订阅，后台生成不受影响。
                if await request.is_disconnected():
                    return
                if event["type"] == "ping":
                    # 心跳走 SSE 注释帧（冒号开头），不会被前端当成业务事件。
                    yield ": ping\n\n"
                else:
                    yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        except AppError as exc:
            # 业务异常转成 error 帧而不是让连接以 500 断开，前端能拿到可读的错误码。
            yield (
                "event: error\ndata: "
                + json.dumps({"type": "error", "message": exc.message, "code": exc.code})
                + "\n\n"
            )

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


class ModelSelection(BaseModel):
    """用户的模型选择请求体。

    accept_external 是对「数据外发到在线模型」的显式确认：选非本机模型却没勾这个标志，
    接口直接拒绝，避免用户在不知情下把对话内容发给第三方。
    """

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
