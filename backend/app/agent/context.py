"""可信运行上下文：仅服务端创建，身份/凭证不属于模型消息或 checkpoint。"""

from dataclasses import dataclass, field


@dataclass
class RunContext:
    user_id: str
    conversation_id: str
    token: str = field(repr=False)
    client: object = field(repr=False)
    repository: object = field(repr=False)
    results: list[dict] = field(default_factory=list)
