"""worker 独立配置：不依赖 HTTP、钱包认证或 Agent 模型设置。"""

from typing import Literal, Self

from pydantic import Field, model_validator

from app.core.settings import EnvSettings
from app.modules.knowledge.config import KnowledgeDataSettings


class WorkerSettings(EnvSettings, KnowledgeDataSettings):
    environment: Literal["development", "test", "production"] = "development"
    knowledge_database_url: str = Field(
        default="postgresql+asyncpg://app_knowledge:copilot_dev_knowledge@127.0.0.1:54329/web3copilot",
        repr=False,
    )
    embedding_ingest_timeout_seconds: float = Field(default=180, gt=0)
    embedding_ingest_batch_size: int = Field(default=8, ge=1, le=64)
    embedding_ingest_base_url: str = ""
    knowledge_worker_poll_seconds: float = Field(default=1, gt=0)
    knowledge_retry_delay_seconds: float = Field(default=5, ge=0)
    knowledge_max_attempts: int = Field(default=3, ge=1)
    knowledge_task_lease_seconds: float = Field(default=60, ge=3)

    @model_validator(mode="after")
    def validate_production(self) -> Self:
        if self.environment == "production" and "copilot_dev_" in self.knowledge_database_url:
            raise ValueError("生产 worker 必须显式配置独立 KNOWLEDGE_DATABASE_URL")
        return self
