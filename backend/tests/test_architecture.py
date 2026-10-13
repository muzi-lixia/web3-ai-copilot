"""部署边界回归，防止基础服务反向依赖 Agent 或工具重新实现业务。"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

# 拦的是 Agent 侧本身：模型编排、消息传递与图执行。
AGENT_SIDE = ("app.agent", "langchain", "langgraph")

# 名字落在 AGENT_SIDE 前缀里，但只是纯文本处理，业务层可以直接用。
TEXT_UTILITIES = ("langchain_text_splitters",)


def is_agent_side(module: str) -> bool:
    return module.startswith(AGENT_SIDE) and not module.startswith(TEXT_UTILITIES)


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


def test_foundation_and_business_do_not_import_agent():
    for directory in ("foundation", "modules"):
        for path in (APP / directory).rglob("*.py"):
            assert not any(is_agent_side(m) for m in imports(path)), path


def test_tools_do_not_import_business_services():
    for path in (APP / "agent" / "tools").rglob("*.py"):
        assert not any(m.startswith("app.modules") for m in imports(path)), path


def test_business_and_infrastructure_do_not_import_routes():
    for directory in ("modules", "infrastructure"):
        for path in (APP / directory).rglob("*.py"):
            assert not any(m.startswith(("app.api", "fastapi")) for m in imports(path)), path


def test_one_agent_runtime_and_no_redis_dependency():
    assert not (APP / "ai").exists()
    assert "create_agent(" in (APP / "agent/service.py").read_text()
    assert "redis" not in (APP.parent / "pyproject.toml").read_text().lower()
