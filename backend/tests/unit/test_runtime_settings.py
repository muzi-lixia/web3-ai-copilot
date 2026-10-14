"""各部署单元只校验自己的配置，不要求其他服务的秘密。"""

import pytest
from pydantic import ValidationError

from app.agent.config import AgentSettings
from app.foundation.config import FoundationSettings
from app.infrastructure.database.config import MigrationSettings
from app.workers.knowledge.config import WorkerSettings


def test_foundation_production_does_not_need_agent_or_migration_settings():
    value = FoundationSettings(
        _env_file=None,
        environment="production",
        debug=False,
        foundation_database_url="postgresql+asyncpg://foundation:secret@db/service",
        jwt_secret="x" * 32,
        siwe_domain="copilot.example",
        cors_origins="https://copilot.example",
    )
    assert not hasattr(value, "agent_database_url")
    assert not hasattr(value, "model_api_key")
    assert not hasattr(value, "database_migration_url")


def test_agent_production_does_not_need_wallet_or_foundation_database():
    value = AgentSettings(
        _env_file=None,
        environment="production",
        debug=False,
        agent_database_url="postgresql+asyncpg://agent:secret@db/service",
        cors_origins="https://copilot.example",
    )
    assert not hasattr(value, "jwt_secret")
    assert not hasattr(value, "foundation_database_url")
    assert not hasattr(value, "rpc_overrides")


def test_migration_only_needs_its_own_database():
    value = MigrationSettings(
        _env_file=None,
        environment="production",
        database_migration_url="postgresql+asyncpg://ddl:secret@db/service",
    )
    assert not hasattr(value, "jwt_secret")
    assert not hasattr(value, "agent_database_url")


def test_worker_has_no_foundation_credential_fallback():
    value = WorkerSettings(_env_file=None)
    assert not hasattr(value, "foundation_database_url")
    assert "app_knowledge" in value.knowledge_database_url
    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None, environment="production")


@pytest.mark.parametrize("provider", ["ollama", "deepseek", "qwen"])
def test_default_and_selected_provider_share_model_configuration(provider):
    from app.agent.llm import select_settings

    kwargs = {
        "model_provider": provider,
        f"{provider}_model": "custom-model",
        f"{provider}_base_url": "http://model.internal:1234",
        f"{provider}_context_window": 24576,
    }
    if provider != "ollama":
        kwargs[f"{provider}_api_key"] = "test-key"
    settings = AgentSettings(_env_file=None, **kwargs)
    selected = select_settings(settings, provider)
    assert settings.model_name == selected.model_name == "custom-model"
    assert settings.model_base_url == selected.model_base_url == "http://model.internal:1234"
    assert settings.model_context_window == selected.model_context_window == 24576


def test_worker_does_not_accept_online_retrieval_configuration():
    value = WorkerSettings(_env_file=None)
    for name in (
        "knowledge_search_min_score", "embedding_query_concurrency",
        "embedding_query_timeout_seconds", "embedding_query_base_url", "knowledge_max_bytes",
    ):
        assert not hasattr(value, name)


def test_database_connections_do_not_fall_back_to_generic_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://wrong-role@db/wrong")
    for cls, field in (
        (FoundationSettings, "foundation_database_url"), (AgentSettings, "agent_database_url"),
    ):
        value = cls(_env_file=None)
        assert "wrong-role" not in getattr(value, field)
        assert not hasattr(value, "database_url")
