"""应用只依赖模型能力，不依赖 Ollama 的 HTTP 请求体或具体 SDK。"""

from collections.abc import AsyncIterator
from typing import Protocol


class ChatModel(Protocol):
    """聊天运行时和记忆策略所需的最小模型能力。

    这是结构化类型协议，不要求继承具体 SDK；新增模型提供方可实现这些方法后
    在容器中注入。流事件属于内部 delta/done 协议，对外 SSE 由 API 层转换。
    """

    def fits_context(self, messages: list[dict], summary: bool = False) -> bool:
        """判断完整输入、输出预留及安全余量是否能放入窗口；summary 选择摘要输出预算。"""
        ...

    def validate_context(self, messages: list[dict], *, summary: bool = False) -> None:
        """输入超出安全范围时抛异常，避免依赖模型自动截断；成功时无返回值。"""
        ...

    def stream_chat(self, messages: list[dict]) -> AsyncIterator[dict]:
        """返回异步事件流：delta 携带新增文本，done 携带完成标记与用量；不得静默断流。"""
        ...

    async def summarize(self, messages: list[dict]) -> dict:
        """对已组装的摘要消息返回解析后的 JSON 对象；结构与出处校验由记忆层完成。"""
        ...
