"""RPC 与外部数据源的配置契约，不加载具体 HTTP 服务。"""

from pydantic import BaseModel, Field


class ProviderSettings(BaseModel):
    rpc_overrides: str = ""
    dexscreener_base_url: str = "https://api.dexscreener.com"
    defillama_base_url: str = "https://coins.llama.fi"
    market_timeout_seconds: float = Field(default=10.0, gt=0)
    market_cache_ttl: int = Field(default=60, gt=0)
    market_max_stale_seconds: int = Field(default=300, ge=0)
    market_proxy: str = ""
    cache_max_entries: int = Field(default=256, ge=1)
    beep_base_url: str = "https://beep.berachain.com"
    beep_client_id: str = "web3aicopilot.api"
    beep_timeout_seconds: float = Field(default=10.0, gt=0)
    beep_cache_ttl: int = Field(default=300, gt=0)

    @property
    def rpc_override_map(self) -> dict[int, list[str]]:
        """把 rpc_overrides 解析成 {chain_id: [url, ...]}。

        格式错误的片段直接跳过而不是抛错：这是部署期的环境变量，
        一个手滑的空格不该让整个服务起不来；跳过时链注册表里的公共节点仍可用。
        """
        out: dict[int, list[str]] = {}
        for chunk in self.rpc_overrides.split(","):
            key, sep, urls = chunk.partition("=")
            if not sep:
                continue
            try:
                chain_id = int(key.strip())
            except ValueError:
                continue
            items = [u.strip() for u in urls.split("|") if u.strip()]
            if items:
                out[chain_id] = items
        return out
