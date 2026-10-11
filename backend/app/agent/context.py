"""可信运行上下文：仅服务端创建，身份/凭证不属于模型消息或 checkpoint。"""

from dataclasses import dataclass, field


@dataclass
class RunContext:
    """一次生成的运行上下文，只由服务端构造。

    身份、凭证与依赖放在这里而不是模型消息或 checkpoint 中：模型既看不到也填不了。
    token 与两个依赖用 repr=False，避免随日志或异常回溯泄露。
    """

    user_id: str
    conversation_id: str
    token: str = field(repr=False)
    client: object = field(repr=False)
    repository: object = field(repr=False)
    # 本轮已登记的结果引用元数据，随快照下发给前端展示。
    results: list[dict] = field(default_factory=list)
