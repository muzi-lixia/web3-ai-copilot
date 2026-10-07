"""SSE 事件契约 —— Agent 可观测性的数据载体。

设计要点：
- `seq` 单调递增，前端据此排序与补发，也是断线重连的依据。
- `result_digest` 只传摘要不传全量，轨迹面板只需要"发生了什么"。
- `RunFailed.partial` 标记"部分数据缺失但结论可用"，对应失败降级策略。
"""

from typing import Annotated, Literal

from pydantic import Field

from app.schemas.common import Schema


class _EventBase(Schema):
    run_id: str
    seq: int


class RunStarted(_EventBase):
    type: Literal["run_started"] = "run_started"
    intent: str


class ToolStarted(_EventBase):
    type: Literal["tool_started"] = "tool_started"
    tool: str
    args: dict


class ToolFinished(_EventBase):
    type: Literal["tool_finished"] = "tool_finished"
    tool: str
    ok: bool
    duration_ms: int
    result_digest: str = Field(description="人类可读摘要，如 '6 assets · $23,482'")


class TokenDelta(_EventBase):
    type: Literal["token_delta"] = "token_delta"
    text: str


class RunFinished(_EventBase):
    type: Literal["run_finished"] = "run_finished"
    answer: str
    usage: dict


class RunFailed(_EventBase):
    type: Literal["run_failed"] = "run_failed"
    error: str
    partial: bool = Field(description="True = 部分数据源失败但结论仍可用")


AgentEvent = Annotated[
    RunStarted | ToolStarted | ToolFinished | TokenDelta | RunFinished | RunFailed,
    Field(discriminator="type"),
]
"""判别联合：Pydantic 按 `type` 字段分发到具体事件类，序列化/反序列化都有类型保证。"""
