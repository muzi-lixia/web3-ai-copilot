"""Agent 独立配置，不加载钱包签名私有配置或基础服务数据库凭据。"""

from functools import lru_cache
from typing import Literal, Self

from pydantic import BaseModel, Field, SecretStr, model_validator

from app.core.settings import EnvSettings, HttpSettings


class ModelSettings(BaseModel):
    model_provider: Literal["ollama", "deepseek", "qwen"] = "ollama"
    ollama_model: str = "qwen2.5:7b"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_context_window: int = Field(default=16384, ge=4096)
    rewrite_provider: Literal["", "ollama"] = ""
    model_proxy: str = Field(default="", repr=False)
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_context_window: int = Field(default=65536, ge=4096)
    qwen_api_key: SecretStr = SecretStr("")
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"
    qwen_context_window: int = Field(default=32768, ge=4096)
    model_output_tokens: int = Field(default=8192, ge=1)
    model_context_reserve: int = Field(default=1024, ge=256)


    @property
    def model_name(self) -> str:
        return getattr(self, f"{self.model_provider}_model")

    @property
    def model_base_url(self) -> str:
        return getattr(self, f"{self.model_provider}_base_url")

    @property
    def model_api_key(self) -> SecretStr:
        if self.model_provider == "ollama":
            return SecretStr("")
        return getattr(self, f"{self.model_provider}_api_key")

    @property
    def model_context_window(self) -> int:
        return getattr(self, f"{self.model_provider}_context_window")


class AgentRuntimeSettings(BaseModel):
    agent_database_url: str = "postgresql+asyncpg://app_agent:copilot_dev_agent@127.0.0.1:54329/web3copilot"
    copilot_snapshot_seconds: float = Field(default=1.0, gt=0)
    copilot_stream_seconds: float = Field(default=0.05, gt=0)
    model_concurrency: int = Field(default=1, ge=1, le=32)
    agent_model_call_limit: int = Field(default=6, ge=1, le=20)
    agent_tool_call_limit: int = Field(default=8, ge=1, le=20)
    chat_queue_limit: int = Field(default=16, ge=1)
    chat_queue_timeout_seconds: float = Field(default=30, gt=0)
    copilot_timeout_seconds: float = Field(default=180, gt=0)
    tool_timeout_seconds: float = Field(default=30, gt=0)
    langsmith_api_key: SecretStr = SecretStr("")
    langsmith_project: str = "web3-ai-copilot"
    langsmith_capture_content: bool = False
    foundation_url: str = "http://127.0.0.1:8000"
    agent_outbox_dir: str = "var/agent-outbox"
    conversation_ttl_hours: int = Field(default=720, ge=1)
    agent_context_tokens: int = Field(default=16384, ge=2048)

    @property
    def required_permit_seconds(self) -> float:
        """一轮最长可能占用的秒数：排队等模型额度 + 执行。

        委托凭证必须覆盖这两段，任一段超出寿命都会在取数时被判凭证过期。
        """
        return self.chat_queue_timeout_seconds + self.copilot_timeout_seconds


class AgentSettings(EnvSettings, HttpSettings, ModelSettings, AgentRuntimeSettings):
    default_chain_id: int = 80094

    @model_validator(mode="after")
    def validate_agent(self) -> Self:
        if self.environment == "production":
            if not self.agent_database_url or "copilot_dev_" in self.agent_database_url:
                raise ValueError("生产 Agent 必须配置独立 AGENT_DATABASE_URL")
            if self.debug or not self.cors_origin_list or "*" in self.cors_origin_list:
                raise ValueError("生产 Agent 须关闭 DEBUG 并明确 CORS_ORIGINS")
        return self


@lru_cache
def get_agent_settings():
    return AgentSettings()
