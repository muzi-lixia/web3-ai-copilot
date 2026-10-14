"""模型外发边界：改写不跨云端提供方，追踪不发送输入输出。"""

from contextlib import asynccontextmanager

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from app.agent import llm
from app.agent.config import AgentSettings
from app.agent.main import enable_tracing
from app.agent.rag.rewriter import recent_dialogue


def test_rewrite_history_removes_asset_round_values():
    history = recent_dialogue([
        HumanMessage(content="我的 BERA 余额"),
        AIMessage(content="", tool_calls=[{"name": "query_assets", "args": {}, "id": "c"}]),
        ToolMessage(content='{"amount":"981234.567"}', tool_call_id="c"),
        AIMessage(content="你的余额为 981234.567 BERA"),
        HumanMessage(content="它能质押吗"),
    ])
    assert "981234.567" not in history
    assert "BERA" in history


@pytest.mark.parametrize("provider", ["deepseek", "qwen"])
def test_independent_cloud_rewriter_configuration_is_rejected(provider):
    with pytest.raises(ValidationError):
        AgentSettings(_env_file=None, rewrite_provider=provider)


async def test_local_rewriter_does_not_change_cloud_main_model(monkeypatch):
    opened = []

    @asynccontextmanager
    async def fake_model(settings):
        opened.append(settings.model_provider)
        yield object()

    monkeypatch.setattr(llm, "chat_model", fake_model)
    settings = AgentSettings(
        _env_file=None, model_provider="deepseek", deepseek_api_key="test", rewrite_provider="ollama"
    )
    async with llm.chat_models(settings) as (main, rewriter, chosen):
        assert main is not rewriter
        assert chosen.model_provider == "ollama"
    assert opened == ["deepseek", "ollama"]


def test_trace_hides_payload_and_disables_unconfigured_tracing(monkeypatch):
    for name in ("LANGSMITH_HIDE_INPUTS", "LANGSMITH_HIDE_OUTPUTS", "LANGSMITH_TRACING",
                 "LANGCHAIN_TRACING_V2"):
        monkeypatch.setenv(name, "true" if "TRACING" in name else "false")
    enable_tracing(AgentSettings(_env_file=None))
    import os

    assert os.environ["LANGSMITH_HIDE_INPUTS"] == "true"
    assert os.environ["LANGSMITH_HIDE_OUTPUTS"] == "true"
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"


def test_content_capture_setting_does_not_get_overridden_by_environment(monkeypatch):
    import os

    for name in ("LANGSMITH_HIDE_INPUTS", "LANGSMITH_HIDE_OUTPUTS"):
        monkeypatch.setenv(name, "true")
    enable_tracing(AgentSettings(_env_file=None, langsmith_capture_content=True))
    assert os.environ["LANGSMITH_HIDE_INPUTS"] == "false"
    assert os.environ["LANGSMITH_HIDE_OUTPUTS"] == "false"
