"""唯一模型配置入口，返回框架标准模型，不实现提供方工具协议。"""

from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

ENDPOINTS = {
    "ollama": "http://127.0.0.1:11434",
    "deepseek": "https://api.deepseek.com",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}


@asynccontextmanager
async def chat_model(settings):
    """每轮固定模型配置快照；退出时关闭客户端，取消亦不泄露连接。"""
    endpoint = settings.model_base_url or ENDPOINTS[settings.model_provider]
    async with AsyncExitStack() as stack:
        if settings.model_provider == "ollama":
            model = ChatOllama(
                model=settings.model_name,
                base_url=endpoint,
                temperature=0,
                num_ctx=settings.model_context_window or 16384,
                num_predict=settings.model_output_tokens,
                client_kwargs={"trust_env": False, "timeout": settings.copilot_timeout_seconds},
            )
            stack.callback(model._client.close)
            stack.push_async_callback(model._async_client.close)
        else:
            sync = stack.enter_context(httpx.Client(proxy=settings.model_proxy or None, trust_env=False))
            client = await stack.enter_async_context(
                httpx.AsyncClient(proxy=settings.model_proxy or None, trust_env=False)
            )
            model = ChatOpenAI(
                model=settings.model_name,
                base_url=endpoint,
                api_key=settings.model_api_key.get_secret_value(),
                max_tokens=settings.model_output_tokens,
                temperature=0,
                max_retries=0,
                stream_usage=True,
                openai_proxy="",
                http_client=sync,
                http_async_client=client,
                timeout=settings.copilot_timeout_seconds,
            )
        yield model


def select_settings(settings, provider):
    """返回每轮独立配置快照；只允许已由部署配置启用的模型，不接受 URL 或 API Key。"""
    from app.core.exceptions import ForbiddenError

    if provider is None or provider == settings.model_provider:
        if settings.model_provider != "ollama" and (
            not settings.model_api_key.get_secret_value() or not settings.model_context_window
        ):
            raise ForbiddenError("当前云端模型尚未配置密钥或上下文窗口")
        return settings.model_copy(deep=True)
    if provider == "ollama":
        return settings.model_copy(
            update={
                "model_provider": "ollama",
                "model_name": "qwen2.5:7b",
                "model_base_url": "",
                "model_context_window": 16384,
            }
        )
    if provider not in ("deepseek", "qwen"):
        raise ForbiddenError("未支持的模型提供方")
    key = getattr(settings, provider + "_api_key")
    if not key.get_secret_value():
        raise ForbiddenError("该模型尚未由管理员配置启用")
    return settings.model_copy(
        update={
            "model_provider": provider,
            "model_name": getattr(settings, provider + "_model"),
            "model_api_key": key,
            "model_base_url": "",
            "model_context_window": getattr(settings, provider + "_context_window"),
        }
    )
