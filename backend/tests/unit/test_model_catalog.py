"""前端模型目录契约：只读、按用户查询，不泄露模型密钥。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.api import selected_model
from app.core.config import Settings


@pytest.mark.parametrize("provider", [None, "ollama", "deepseek"])
async def test_model_catalog_is_private_read_only(provider):
    settings = Settings(_env_file=None, deepseek_api_key="private-key-for-test")
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
    assert result["data"]["provider"] == (provider or settings.model_provider)
    assert "private-key-for-test" not in str(result)
    assert session.scalar.await_args.args[1] == {"u": "current-user"}
    entries = {entry["provider"]: entry for entry in result["data"]["available"]}
    assert entries["deepseek"]["enabled"] is True
    assert entries["qwen"]["enabled"] is False
