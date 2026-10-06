"""全局配置。

约定：所有外部依赖的地址 / key 只在这里读 .env，其他模块一律 `from app.core.config import settings`，
不允许散落 `os.getenv`。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── 应用 ──────────────────────────────────────────────
    app_name: str = "Web3 AI Copilot"
    debug: bool = True
    api_prefix: str = "/api/v1"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ── 链 ────────────────────────────────────────────────
    eth_rpc_url: str = "https://eth.llamarpc.com"
    # 逗号分隔。主节点失败时按序尝试（Phase A 的 chain.py 用）
    eth_rpc_fallback_urls: str = "https://rpc.ankr.com/eth,https://cloudflare-eth.com"
    chain_id: int = 1

    # ── 行情 ──────────────────────────────────────────────
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    coingecko_api_key: str = ""
    market_cache_ttl: int = 60  # 秒

    # ── LLM（Phase C 启用）────────────────────────────────
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_model: str = "gpt-4o-mini"

    # ── 自有锁仓合约（Phase D 部署 Sepolia 后填）───────────
    staking_contract_address: str = ""

    @property
    def rpc_urls(self) -> list[str]:
        """主 RPC + 备用 RPC，按序失败转移。"""
        fallbacks = [u.strip() for u in self.eth_rpc_fallback_urls.split(",") if u.strip()]
        return [self.eth_rpc_url, *fallbacks]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
