"""部署边界回归，防止基础服务反向依赖 Agent 或工具重新实现业务。"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


def test_foundation_and_business_do_not_import_agent():
    for directory in ("foundation", "modules"):
        for path in (APP / directory).rglob("*.py"):
            assert not any(m.startswith(("app.agent", "langchain", "langgraph")) for m in imports(path)), path


def test_tools_do_not_import_business_services():
    assert not any(m.startswith("app.modules") for m in imports(APP / "agent/tools.py"))


def test_business_and_infrastructure_do_not_import_routes():
    for directory in ("modules", "infrastructure"):
        for path in (APP / directory).rglob("*.py"):
            assert not any(m.startswith(("app.api", "fastapi")) for m in imports(path)), path


def test_one_agent_runtime_and_no_redis_dependency():
    assert not (APP / "ai").exists()
    assert "create_agent(" in (APP / "agent/service.py").read_text()
    assert "redis" not in (APP.parent / "pyproject.toml").read_text().lower()
