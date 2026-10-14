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
            assert not any(m.startswith(("app.foundation.api", "fastapi")) for m in imports(path)), path


def test_one_agent_runtime_and_no_redis_dependency():
    assert not (APP / "ai").exists()
    assert "create_agent(" in (APP / "agent/execution.py").read_text()
    assert "redis" not in (APP.parent / "pyproject.toml").read_text().lower()


def test_http_and_business_do_not_import_worker_runtime():
    for directory in ("foundation", "modules", "core"):
        for path in (APP / directory).rglob("*.py"):
            assert not any(m.startswith("app.workers") for m in imports(path)), path


def test_workers_do_not_import_http_or_global_application_settings():
    for path in (APP / "workers").rglob("*.py"):
        assert not any(
            m.startswith(("app.foundation", "app.foundation.api", "app.agent", "fastapi"))
            for m in imports(path)
        ), path


def test_http_import_graph_excludes_document_processing_libraries():
    """检查传递依赖，防止通过共享模块间接加载离线解析链路。"""
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
import app.foundation.main
for module in sys.modules:
    blocked = ("app.workers", "app.agent", "pdfplumber", "docx", "langchain_text_splitters")
    assert not module.startswith(blocked), module
""",
        ],
        cwd=APP.parent,
        check=True,
        capture_output=True,
        text=True,
    )


def test_foundation_owns_its_http_routes_and_container():
    assert (APP / "foundation/api/v1/auth.py").exists()
    assert (APP / "foundation/container.py").exists()
    assert not (APP / "api").exists()


def test_shared_and_infrastructure_do_not_import_service_applications():
    for directory in ("core", "common", "infrastructure"):
        for path in (APP / directory).rglob("*.py"):
            assert not any(m.startswith(("app.foundation", "app.agent", "app.workers"))
                           for m in imports(path)), path


def test_agent_does_not_import_foundation_transport():
    for path in (APP / "agent").rglob("*.py"):
        assert not any(m.startswith("app.foundation") for m in imports(path)), path


def test_shared_settings_only_contain_transport_and_loader_fields():
    from app.core.settings import HttpSettings

    assert not set(HttpSettings.model_fields) & {
        "jwt_secret", "model_provider", "agent_database_url", "rpc_overrides"
    }
