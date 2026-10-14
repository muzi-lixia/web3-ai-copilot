"""运行配置的共享字段组；此模块不创建配置实例，不加载任何应用。"""

from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )


class HttpSettings(BaseModel):
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = True
    api_prefix: str = "/api/v1"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        """将逗号分隔的来源配置转换为列表，并去掉空项和首尾空格。"""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
