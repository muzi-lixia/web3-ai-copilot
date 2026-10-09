"""分层边界和应用装配回归：防止后续扩展重新引入 HTTP 耦合、全局资源和启动绕过。"""

import ast
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.core.exceptions import ChatUnavailableError
from app.main import create_app

APP = Path(__file__).resolve().parents[1] / "app"


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


@pytest.mark.parametrize("layer", ["modules", "ai", "infrastructure", "common"])
def test_inner_layers_do_not_depend_on_http(layer):
    directory = APP / layer
    assert directory.is_dir(), directory
    for path in directory.rglob("*.py"):
        for module in imports(path):
            assert not module.startswith(("app.api", "fastapi", "starlette")), (path, module)


def test_infrastructure_does_not_import_business_services():
    for path in (APP / "infrastructure").rglob("*.py"):
        assert not any(
            module.startswith("app.modules") and ".service" in module for module in imports(path)
        ), path


def test_routes_do_not_bypass_application_services():
    for path in (APP / "api/v1").glob("*.py"):
        assert not any(
            module.startswith(("app.infrastructure", "app.ai.llm"))
            or module.endswith((".repository", ".models"))
            for module in imports(path)
        ), path


async def test_factories_own_independent_resources_and_auth_state():
    first, second = create_app(), create_app()
    a, b = first.state.services, second.state.services
    try:
        assert a.chat is not b.chat and a.database.engine is not b.database.engine
        assert a.repository.database is a.database
        assert a.memory.model is a.model and a.chat.memory is a.memory
        address = "0x" + "1" * 40
        a.auth.issue_nonce(address)
        assert address in a.auth._nonces and address not in b.auth._nonces
        with pytest.raises(ChatUnavailableError):
            await a.chat.submit_message(address, uuid4(), "尚未启动")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url="http://test"
        ) as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/ready")).status_code == 503
        schema = first.openapi()
        assert schema["paths"]["/api/v1/chat/session/messages"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"].endswith("/ApiResponse_MessagePage_")
    finally:
        await a.close()
        await b.close()


def test_business_modules_do_not_depend_on_ai():
    for path in (APP / "modules").rglob("*.py"):
        assert not any(module.startswith("app.ai") for module in imports(path)), path


def test_old_horizontal_packages_are_removed():
    for name in ("services", "schemas", "repositories", "db", "config", "infra", "memory", "llm", "shared"):
        assert not (APP / name).exists(), name
