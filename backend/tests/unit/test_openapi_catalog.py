"""接口目录来自真实 FastAPI 注册信息；公开与私有鉴权文档不能混淆。"""

from types import SimpleNamespace

from app.agent.main import create_app as agent_app
from app.core.config import Settings
from app.foundation.main import create_app as foundation_app


def test_service_catalog_metadata():
    settings = Settings(_env_file=None)
    foundation = foundation_app(SimpleNamespace(settings=settings)).openapi()
    paths = foundation["paths"]
    assert paths["/api/v1/auth/refresh"]["post"]["x-auth-description"] == "Refresh Token（请求体）"
    assert paths["/api/v1/me/balance-results"]["post"]["security"]
    assert paths["/api/v1/me/balance-results"]["delete"]["summary"] == "删除本人资产查询结果"
    quotes = paths["/api/v1/markets/quotes"]["get"]
    assert {} in quotes["security"]
    assert "匿名可访问" in quotes["x-auth-description"]
    assert quotes["tags"] == ["行情"]
    agent = agent_app(settings).openapi()
    model = agent["paths"]["/api/v1/chat/session/model"]
    assert model["get"]["summary"] == "读取模型配置"
    assert model["put"]["summary"] == "切换本人对话模型"
    assert model["get"]["security"]
