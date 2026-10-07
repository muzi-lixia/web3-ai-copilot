"""模型出口的单测。

只测不碰网络的部分。真连不连得上、模型名有没有拼错必须实打 —— 用 `scripts/check_model.py`。
"""

from __future__ import annotations

import pytest

from app.llm import client as model


@pytest.fixture(autouse=True)
def isolated_model_environment(monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
                "MODEL_NAME", "MODEL_PROVIDER", "MODEL_TIMEOUT_SECONDS",
                "DEEPSEEK_BASE_URL", "QWEN_BASE_URL", "OPENAI_BASE_URL", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(key, raising=False)


# ── 平台 ──────────────────────────────────────────────────────


def test_default_is_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认必须是本地 —— 唯一不需要 key 的选项，新克隆的人不用先申请凭证。"""
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    assert model._platform(None) == "ollama"


def test_platform_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量要能覆盖，且不区分大小写、容忍两侧空格。"""
    monkeypatch.setenv("MODEL_PROVIDER", "  DeepSeek  ")
    assert model._platform(None) == "deepseek"


def test_unknown_platform_lists_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """名字写错要立刻报错并列出可选项，不静默兜底到别的平台。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    with pytest.raises(ValueError) as excinfo:
        model.get_client("没这家")
    assert "没这家" in str(excinfo.value)
    for name in model.PLATFORMS:
        assert name in str(excinfo.value)


# ── 模型名 ────────────────────────────────────────────────────


def test_default_model_per_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-y")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-z")
    assert model.get_client("deepseek").model_name == "deepseek-chat"
    assert model.get_client("qwen").model_name == "qwen-plus"
    assert model.get_client("openai").model_name == "gpt-4o-mini"
    assert model.get_client("ollama").model_name == "qwen2.5:7b"


def test_model_name_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_NAME", "qwen-max")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-y")
    assert model.get_client("qwen").model_name == "qwen-max"


# ── 客户端 ────────────────────────────────────────────────────


def test_each_platform_hits_its_own_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """各平台地址要落在自己的域名上 —— 串了就是拿 A 家的 key 去求 B 家。"""
    for env in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(env, "sk-x")
    expect = {
        "deepseek": "api.deepseek.com",
        "qwen": "dashscope.aliyuncs.com",
        "openai": "api.openai.com",
        "ollama": "127.0.0.1:11434",
    }
    for platform, host in expect.items():
        assert host in str(model.get_client(platform).openai_api_base), platform


def test_missing_key_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 key 要在**构造时**就报，并指出该填哪个变量。

    等到第一次提问才 401 的话，排查会先跑偏到业务代码上。
    """
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError) as excinfo:
        model.get_client("deepseek")
    assert "DEEPSEEK_API_KEY" in str(excinfo.value)


def test_kwargs_reach_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """额外参数要真的绑到 client 上。

    LangChain 的坑在这里：字段名拼错**不报错**，只给一条 warning 就塞进 `model_kwargs`，
    参数等于没生效。所以钉一条「传进去 = 读得到」。
    """
    monkeypatch.delenv("MODEL_NAME", raising=False)
    llm = model.get_client("ollama", temperature=0.3, max_tokens=64)
    assert llm.temperature == 0.3
    assert llm.max_tokens == 64
