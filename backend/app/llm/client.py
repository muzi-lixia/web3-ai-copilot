"""LLM 客户端出口。

    from app.llm.client import get_client

    llm = get_client()                                    # 按 MODEL_PROVIDER 选
    resp = await llm.ainvoke([{"role": "user", "content": "你好"}])
    async for chunk in llm.astream([...]):
        print(chunk.content, end="")

四家都提供 OpenAI 兼容的 /chat/completions，所以统一用 `ChatOpenAI` + `base_url` 切平台，
换平台只改 MODEL_PROVIDER：

    deepseek   DEEPSEEK_API_KEY     deepseek-chat
    qwen       DASHSCOPE_API_KEY    qwen-plus     （通义用阿里的变量名，不是 QWEN_API_KEY）
    openai     OPENAI_API_KEY       gpt-4o-mini
    ollama     —                    qwen2.5:7b

模型名、temperature、max_tokens 都绑在 client 上，取一次到处用；其余参数走 `**kwargs` 透传。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# 平台 -> (默认地址, key 的环境变量名, 默认模型)。key 名为空 = 不需要 key。
PLATFORMS: dict[str, tuple[str, str, str]] = {
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", "qwen-plus"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-4o-mini"),
    "ollama": ("http://127.0.0.1:11434/v1", "", "qwen2.5:7b"),
}


def _platform(name: str | None) -> str:
    """定平台：参数 > MODEL_PROVIDER > 本地 Ollama（唯一不需要 key 的选项）。"""
    key = (name or os.getenv("MODEL_PROVIDER") or "ollama").strip().lower()
    if key not in PLATFORMS:
        raise ValueError(f"未知平台 {key!r}，可选：{', '.join(PLATFORMS)}（改 MODEL_PROVIDER）")
    return key


def get_client(platform: str | None = None, **kwargs: object) -> ChatOpenAI:
    """取客户端。缺 key 在这里就报 —— 等到第一次提问才 401 的话，排查会先跑偏到业务代码上。

    地址可用 `{平台名大写}_BASE_URL` 覆盖（自建网关、把 Ollama 指向局域网另一台机器）。
    其余参数直接透传给 `ChatOpenAI`，例如 `get_client(temperature=0.2, max_tokens=1024)`。

    注意超时字段在 LangChain 里叫 `request_timeout`，不叫 `timeout` —— 拼错不会报错，
    只会带着 warning 落进 `model_kwargs`，超时仍按默认值走。
    """
    name = _platform(platform)
    base_url, key_env, default_model = PLATFORMS[name]

    api_key = os.getenv(key_env, "") if key_env else "not-needed"
    if key_env and not api_key:
        raise ValueError(f"平台 {name} 缺 key：环境变量 {key_env} 没有值")

    # 默认走在前面，kwargs 放后面 —— 调用方能覆盖超时（脚本里跑本地小模型要放宽到 180s）。
    options: dict[str, object] = {
        "request_timeout": float(os.getenv("MODEL_TIMEOUT_SECONDS") or 60),
        **kwargs,
    }
    return ChatOpenAI(
        model=os.getenv("MODEL_NAME") or default_model,
        api_key=api_key,
        base_url=os.getenv(f"{name.upper()}_BASE_URL") or base_url,
        **options,  # type: ignore[arg-type]
    )
