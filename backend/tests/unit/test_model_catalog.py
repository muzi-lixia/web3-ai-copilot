"""前端模型目录契约：只读、按用户查询，不泄露模型密钥。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.api import selected_model
from app.agent.config import AgentSettings
from app.agent.llm import resolve_session_provider, select_settings


@pytest.mark.parametrize("provider", [None, "ollama", "deepseek"])
@pytest.mark.parametrize("deployment", ["ollama", "deepseek"])
async def test_model_catalog_is_private_read_only(provider, deployment):
    settings = AgentSettings(
        _env_file=None, model_provider=deployment, deepseek_api_key="private-key-for-test",
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=provider))
    users = []

    @asynccontextmanager
    async def transaction(user):
        users.append(user)
        yield session

    services = SimpleNamespace(
        settings=settings, repository=SimpleNamespace(db=SimpleNamespace(transaction=transaction))
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=services)))
    result = await selected_model(request, {"user_id": "current-user"})
    assert users == ["current-user"]
    assert result["code"] == 0
    assert result["data"]["provider"] == (provider or "ollama")
    assert "private-key-for-test" not in str(result)
    assert session.scalar.await_args.args[1] == {"u": "current-user"}
    entries = {entry["provider"]: entry for entry in result["data"]["available"]}
    assert entries["deepseek"]["enabled"] is True
    assert entries["qwen"]["enabled"] is False


@pytest.mark.parametrize("deployment", ["ollama", "deepseek", "qwen"])
@pytest.mark.parametrize("provider", [None, "ollama", "deepseek", "qwen"])
def test_session_provider_requires_explicit_cloud_selection(deployment, provider):
    settings = AgentSettings(_env_file=None, model_provider=deployment)
    assert resolve_session_provider(settings, provider) == (provider or "ollama")


def test_cloud_deployment_uses_configured_local_model_for_new_session():
    settings = AgentSettings(
        _env_file=None, model_provider="deepseek", ollama_model="custom-local",
        ollama_base_url="http://localhost:12000", ollama_context_window=32768,
    )
    chosen = select_settings(settings, resolve_session_provider(settings, None))
    assert chosen.model_provider == "ollama"
    assert chosen.model_name == "custom-local"
    assert chosen.model_base_url == "http://localhost:12000"
    assert chosen.model_context_window == 32768


def test_local_deployment_preserves_existing_model_configuration():
    settings = AgentSettings(
        _env_file=None, ollama_model="existing-local", ollama_base_url="http://localhost:12001",
        ollama_context_window=32768,
    )
    chosen = select_settings(settings, resolve_session_provider(settings, None))
    assert chosen.model_name == "existing-local"
    assert chosen.model_base_url == "http://localhost:12001"
    assert chosen.model_context_window == 32768
