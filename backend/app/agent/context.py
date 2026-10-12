"""可信运行上下文：仅服务端创建，身份/凭证不属于模型消息或 checkpoint。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


async def _no_persist():
    """默认的即时落库回调：什么都不做。

    只有 generate() 构造的上下文才注入真实实现，其它路径（测试等）拿到的是这个空实现。
    """


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
    # 本轮已登记的结果（信封 + body），随快照下发给前端展示。
    results: list[dict] = field(default_factory=list)
    # 本轮的工具调用轨迹（工具名 + 参数 + 耗时 + 状态）：落库排查，也作为 tool_meta 下发前端。
    # 不进入模型消息 —— 模型只看到各工具自己的返回值。
    tool_calls: list[dict] = field(default_factory=list)
    # 工具产出新结果时立即落库并唤醒订阅端：卡片不必等 1 秒的落库刻度。
    # 由 generate() 注入；默认空实现，便于测试直接构造上下文。
    persist: Callable[[], Awaitable[None]] = field(default=_no_persist, repr=False)
