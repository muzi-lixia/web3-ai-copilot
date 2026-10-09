"""服务端会话接口的请求契约。

前端只提交问题和幂等 ID，不再提交 history 或钱包地址；上下文与身份分别由后端和 JWT 决定。
新增生成与重试拒绝额外字段，避免旧版客户端静默继续发送已经失效的参数。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TurnRequest(BaseModel):
    """新问题只接受正文和客户端幂等 ID；同一请求重发必须复用该 ID。"""

    model_config = ConfigDict(extra="forbid")
    message: str | None = Field(default=None, min_length=1, max_length=2000)
    retry_of: UUID | None = None
    # 同一次发送的网络重试必须复用；重新生成属于新尝试，需要新的 UUID。
    client_msg_id: UUID

    @field_validator("message")
    @classmethod
    def nonempty(cls, value):
        """清除首尾空白并拒绝纯空白问题，避免创建没有有效内容的轮次。"""
        if value is None:
            return value
        if not value.strip():
            raise ValueError("消息不能为空")
        return value.strip()

    @model_validator(mode="after")
    def one_source(self):
        """新问题和重试引用必须且只能提供一个，避免客户端混改重试原问题。"""
        if (self.message is None) == (self.retry_of is None):
            raise ValueError("message 与 retry_of 必须且只能提供一个")
        return self


# 当前快照 SSE 的实际契约；不保留尚未实现的 Agent 工具事件类型。
class SnapshotMessage(BaseModel):
    """一条可展示的完整消息。

    id 用于替换正文，turn_id 区分生成尝试，seq 是会话内排序序号。content
    为累计正文而非 token 增量；重连拿到最新快照即可恢复展示。
    """

    id: UUID
    turn_id: UUID
    seq: int
    role: Literal["user", "assistant"]
    content: str
    status: str


class TurnSnapshot(BaseModel):
    """单次生成尝试的公开状态快照。

    version 随正文或状态变化增长，客户端据此丢弃迟到快照；summary_version
    记录生成时的摘要版本。persistence_pending 表示终态尚需补写，不能当作已落库成功。
    context_info 在 HTTP 返回前移除内部 model_messages，仅保留预算及降级信息。
    """

    turn_id: UUID
    session_id: UUID
    status: Literal["running", "completed", "truncated", "failed", "cancelled", "interrupted"]
    version: int
    message: SnapshotMessage
    model: str
    error: str | None = None
    context_info: dict = Field(default_factory=dict)
    summary_version: int | None = None
    usage: dict = Field(default_factory=dict)
    persistence_pending: bool = False


class SnapshotEvent(TurnSnapshot):
    """SSE 业务载荷：运行中为 snapshot，终态为 done；两者均携带完整正文快照。"""

    type: Literal["snapshot", "done"]


class StreamErrorEvent(BaseModel):
    """订阅连接建立后的错误载荷，区别于生成轮次自身的 failed 状态。"""

    type: Literal["error"] = "error"
    message: str
    code: str = "stream_error"


class SessionResponse(BaseModel):
    """幂等打开当前用户唯一会话的结果；返回稳定 ID，不提供多会话创建和切换能力。"""

    id: UUID


class TurnAccepted(BaseModel):
    """后台生成接受结果。created=False 表示幂等命中旧请求，不会再启动一次推理。"""

    turn_id: UUID
    created: bool


class MessagePage(BaseModel):
    """历史分页结果及恢复信息。

    items 按时间正序展示，next_cursor 用于请求更早消息，active_turn_id 用于
    恢复正在运行的订阅；summary_version 仅表示数据库当前最新摘要版本。
    """

    items: list[SnapshotMessage]
    # 传回 before_seq 读取更早消息；None 表示已经没有前一页。
    next_cursor: int | None
    active_turn_id: UUID | None
    summary_version: int | None


class SummaryState(BaseModel):
    """仓储返回的只读摘要数据，业务层不接收 SQLAlchemy 实体或依赖其会话状态。"""

    model_config = ConfigDict(from_attributes=True)
    version: int
    covered_through_seq: int
    content: dict
    # 记录摘要所见原文的 ID 范围，条目 sources 必须引用这个集合。
    source_message_ids: list[str]
