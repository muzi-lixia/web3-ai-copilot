"""默认入口为独立基础服务；Agent 使用 app.agent.main:app 单独启动。"""

from app.foundation.main import app, create_app

__all__ = ["app", "create_app"]
