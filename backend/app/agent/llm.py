"""唯一模型配置入口，返回框架标准模型，不实现提供方工具协议。"""

from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

LOCAL_PROVIDER = "ollama"


def resolve_session_provider(settings, provider):
    """显式会话选择优先；未选择时使用本地提供方，避免继承云端部署默认值。"""
    if provider is not None:
        return provider
    if settings.model_provider == LOCAL_PROVIDER:
        return settings.model_provider
    return LOCAL_PROVIDER


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


@asynccontextmanager
async def chat_models(settings):
    """打开本轮要用的模型：主模型、改写模型、改写模型自己的配置。

    改写未配置独立提供方（`settings.rewrite_provider` 为空）、或配的就是主模型的提供方时，
    改写与主模型共用同一个实例，不另建连接。独立云端提供方不启用，避免绕过用户选择。
    """
    async with chat_model(settings) as model:
        provider = settings.rewrite_provider
        # 独立云端改写不能绕过用户选定的提供方；仅允许独立本地改写。
        if provider != "ollama" or provider == settings.model_provider:
            yield model, model, settings
            return
        chosen = select_settings(settings, provider)
        async with chat_model(chosen) as rewriter:
            yield model, rewriter, chosen


def without_thinking(model, settings):
    """返回关闭思考链的模型副本；提供方不支持该参数时原样返回。"""
    if settings.model_provider != "qwen":
        return model
    body = {**(model.extra_body or {}), "enable_thinking": False}
    return model.model_copy(update={"extra_body": body})


def select_settings(settings, provider):
    """返回每轮独立配置快照；只允许已由部署配置启用的模型，不接受 URL 或 API Key。"""
    from app.core.exceptions import ForbiddenError

    provider = provider or settings.model_provider
    if provider not in ("ollama", "deepseek", "qwen"):
        raise ForbiddenError("未支持的模型提供方")
    chosen = settings.model_copy(update={"model_provider": provider}, deep=True)
    if provider != LOCAL_PROVIDER and not chosen.model_api_key.get_secret_value():
        raise ForbiddenError("该模型尚未由管理员配置启用")
    return chosen
