"""Foundation 资源与业务服务装配；路由和业务模块不负责创建应用容器。"""

from dataclasses import dataclass

from app.foundation.config import FoundationSettings
from app.infrastructure.database.session import Database
from app.infrastructure.resources import BusinessResources
from app.modules.asset.results import ResultService
from app.modules.identity.service import AuthService
from app.modules.knowledge.service import KnowledgeService


@dataclass(slots=True)
class FoundationServices:
    settings: FoundationSettings
    database: Database
    resources: BusinessResources
    auth: AuthService
    results: ResultService
    knowledge: KnowledgeService


def build_services(settings: FoundationSettings) -> FoundationServices:
    database = Database(settings.foundation_database_url)
    resources = BusinessResources(settings)
    return FoundationServices(
        settings=settings, database=database, resources=resources,
        auth=AuthService(database, settings), results=ResultService(database, resources),
        knowledge=KnowledgeService(database, settings),
    )
