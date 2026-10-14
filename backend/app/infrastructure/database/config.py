"""结构迁移专属配置，不加载 HTTP、Agent 或钱包配置。"""

from typing import Literal, Self

from pydantic import model_validator

from app.core.settings import EnvSettings


class MigrationSettings(EnvSettings):
    environment: Literal["development", "test", "production"] = "development"
    database_migration_url: str = "postgresql+asyncpg://app_ddl:copilot_dev_ddl@127.0.0.1:54329/web3copilot"

    @model_validator(mode="after")
    def validate_migration(self) -> Self:
        if self.environment == "production" and "copilot_dev_" in self.database_migration_url:
            raise ValueError("生产迁移必须显式配置 DATABASE_MIGRATION_URL")
        return self
