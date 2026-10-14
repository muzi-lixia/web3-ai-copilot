"""基础 HTTP 服务的运行配置，仅校验自身依赖。"""

from functools import lru_cache
from typing import Self

from pydantic import BaseModel, Field, model_validator

from app.core.settings import EnvSettings, HttpSettings
from app.infrastructure.providers.config import ProviderSettings
from app.modules.knowledge.config import KnowledgeSettings


class IdentitySettings(BaseModel):
    jwt_secret: str = "dev-only-insecure-secret-change-me-before-deploy"
    nonce_ttl_seconds: int = Field(default=300, gt=0)
    auth_rate_per_minute: int = Field(default=30, gt=0)
    siwe_domain: str = "localhost:5173"
    siwe_uri: str = "http://localhost:5173"
    access_token_minutes: int = Field(default=15, ge=1, le=60)
    login_session_days: int = Field(default=7, ge=1, le=30)
    agent_permit_seconds: int = Field(default=300, ge=30, le=3600)


class BusinessSettings(ProviderSettings):
    default_chain_id: int = 80094
    request_limit_per_minute: int = Field(default=60, ge=1)
    anonymous_limit_per_minute: int = Field(default=20, ge=1)
    admin_addresses: str = ""
    asset_result_ttl_hours: int = Field(default=720, ge=1)

    @property
    def admin_address_set(self) -> set[str]:
        return {a.strip().lower() for a in self.admin_addresses.split(",") if a.strip()}


class FoundationSettings(EnvSettings, HttpSettings, BusinessSettings, IdentitySettings, KnowledgeSettings):
    foundation_database_url: str = (
        "postgresql+asyncpg://app_foundation:copilot_dev_foundation@127.0.0.1:54329/web3copilot"
    )

    @model_validator(mode="after")
    def validate_foundation(self) -> Self:
        if self.environment == "production":
            if not self.foundation_database_url or "copilot_dev_" in self.foundation_database_url:
                raise ValueError("生产基础服务必须配置独立 FOUNDATION_DATABASE_URL")
            if self.debug or not self.cors_origin_list or "*" in self.cors_origin_list:
                raise ValueError("生产基础服务须关闭 DEBUG 并明确 CORS_ORIGINS")
            if self.jwt_secret.startswith("dev-only") or len(self.jwt_secret.strip().encode()) < 32:
                raise ValueError("生产 JWT_SECRET 须至少 32 字节")
            if self.siwe_domain.split(":")[0] in {"localhost", "127.0.0.1", ""}:
                raise ValueError("生产基础服务须配置 SIWE_DOMAIN")
        return self


@lru_cache
def get_foundation_settings():
    return FoundationSettings()
