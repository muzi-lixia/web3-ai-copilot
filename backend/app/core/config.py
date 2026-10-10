"""全局配置：外部依赖地址、密钥及业务参数统一从 Settings 读取。"""

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置及启动时约束。

    Pydantic 从默认值、.env 和环境变量加载配置，环境变量可覆盖本地文件。
    所有预算、超时及容量在这里集中定义；生产校验用于阻止开发默认值直接上线。
    extra=ignore 兼容环境中其他变量，hide_input_in_errors 避免校验错误回显敏感输入。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    # ── 应用 ──────────────────────────────────────────────
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = True
    api_prefix: str = "/api/v1"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # 运行账号只有业务表读写权；迁移账号负责 DDL，部署时两类凭据应分别提供。
    database_url: str = "postgresql+asyncpg://app_rw:copilot_dev_rw@127.0.0.1:54329/web3copilot"
    foundation_database_url: str | None = None
    agent_database_url: str | None = None
    database_migration_url: str = "postgresql+asyncpg://app_ddl:copilot_dev_ddl@127.0.0.1:54329/web3copilot"
    # 周期保存正文快照；结束/失败/取消另行立即保存，避免仅靠周期任务遗漏终态。
    copilot_snapshot_seconds: float = Field(default=1.0, gt=0)

    # 工厂选择提供方；云端模型窗口必须按部署明确配置，不能沿用本地默认值。
    model_provider: Literal["ollama", "deepseek", "qwen"] = "ollama"
    model_name: str = "qwen2.5:7b"
    model_base_url: str = ""
    # 云端模型独立显式代理；留空直连，不读取 HTTP_PROXY/HTTPS_PROXY。
    model_proxy: str = Field(default="", repr=False)
    model_api_key: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_model: str = "deepseek-chat"
    deepseek_context_window: int = Field(default=65536, ge=4096)
    qwen_api_key: SecretStr = SecretStr("")
    qwen_model: str = "qwen-plus"
    qwen_context_window: int = Field(default=32768, ge=4096)
    model_context_window: int | None = Field(default=None, ge=4096)
    model_output_tokens: int = Field(default=1024, ge=1)
    model_context_reserve: int = Field(default=1024, ge=256)
    # 本地默认串行；云端可按部署容量增加并发。
    model_concurrency: int = Field(default=1, ge=1, le=32)
    agent_model_call_limit: int = Field(default=3, ge=1, le=20)
    agent_tool_call_limit: int = Field(default=4, ge=1, le=20)
    chat_queue_limit: int = Field(default=16, ge=1)
    chat_queue_timeout_seconds: float = Field(default=30, gt=0)
    copilot_timeout_seconds: float = Field(default=180, gt=0)
    tool_timeout_seconds: float = Field(default=30, gt=0)

    # ── 链 ────────────────────────────────────────────────
    # 链本身的参数（chain_id / 原生币 / Multicall 地址 / 公共 RPC / 浏览器）
    # 不在 .env 里，而在 app/infrastructure/blockchain/chains.py。这里是「选择」与「覆盖」：
    default_chain_id: int = 80094
    """请求不带 chain_id 时用哪条链。可选项见 chains.py。"""

    rpc_overrides: str = ""
    """覆盖链注册表里的公共 RPC，形如 `80094=https://a|https://b,1=https://c`。

    留空则用 chains.py 里的公共节点。**带 key 的私有节点必须走这里** ——
    chains.py 是要提交到仓库的文件，把 key 写进去等于泄漏。
    同一条链的多个 URL 用 `|` 分隔，按序失败转移。
    """

    # ── 行情 ──────────────────────────────────────────────
    # 两个源互补，都按「链标识 + 合约地址」查询，**不需要维护任何第三方 coin id**
    # —— 清单里加一行 TokenMeta 就自动有行情，与链/币可配置的口径一致：
    #   DexScreener  四个字段全（价格 / 24h 涨跌 / 市值 / 24h 成交量），主源
    #   DefiLlama    只有价格，但收录了 BGT 这类没有 DEX 池子的币，补位
    dexscreener_base_url: str = "https://api.dexscreener.com"
    defillama_base_url: str = "https://coins.llama.fi"
    market_timeout_seconds: float = Field(default=10.0, gt=0)
    market_cache_ttl: int = Field(default=60, gt=0)
    market_max_stale_seconds: int = Field(default=300, ge=0)
    """行情缓存秒数。免费源有限流，且行情不需要秒级新鲜度。"""
    market_proxy: str = ""
    """行情请求专用代理。留空 = 跟随环境变量（HTTP_PROXY / HTTPS_PROXY）。

    单独给一个开关的原因：本机环境的代理可能对某些域名工作、对另一些不工作，
    环境变量在这种混杂情况下不可靠，显式配置才能稳定复现。示例：http://127.0.0.1:7890
    """

    # ── 钱包登录（SIWE 式：nonce → 签名 → JWT）───────────────
    jwt_secret: str = "dev-only-insecure-secret-change-me-before-deploy"
    """JWT 签名密钥。

    默认值只够本地跑通，上线必须换成随机值，否则任何人都能伪造 token。
    生成：`python -c "import secrets; print(secrets.token_urlsafe(48))"`
    HS256 要求密钥不短于 32 字节，短了 PyJWT 会告警。
    """
    nonce_ttl_seconds: int = Field(default=300, gt=0)
    auth_rate_per_minute: int = Field(default=30, gt=0)
    """nonce 有效期 5 分钟。签名是一次性动作，过期即作废。"""
    siwe_domain: str = "localhost:5173"
    siwe_uri: str = "http://localhost:5173"
    access_token_minutes: int = Field(default=15, ge=1, le=60)
    login_session_days: int = Field(default=7, ge=1, le=30)
    foundation_url: str = "http://127.0.0.1:8000"
    conversation_ttl_hours: int = Field(default=720, ge=1)
    request_limit_per_minute: int = Field(default=60, ge=1)
    anonymous_limit_per_minute: int = Field(default=20, ge=1)
    cache_max_entries: int = Field(default=256, ge=1)
    agent_context_tokens: int = Field(default=8192, ge=2048)
    """写入签名消息的域名，让用户在钱包里看到「我在向谁授权」。"""

    # ── 质押 ──────────────────────────────────────────────
    # 质押的 APY 与收益只有 Berachain 自己算得出来（它是 PoL 激励回购后注入金库形成的），
    # DexScreener / DefiLlama 都给不出。Beep 就是 BeraHub 前端调的那个数据层，
    # 所以这里的数就是页面上的那个数。
    beep_base_url: str = "https://beep.berachain.com"
    beep_client_id: str = "web3aicopilot.api"
    """Beep 要求的调用方标识（走 `X-Client-Id` 头）。

    不缺校验、也不用注册，但要送一个稳定的小写 `<产品>.<模块>` 标识 ——
    缺失会被直接拒（400）。它属于部署环境而不是代码，所以放这里。
    """
    beep_timeout_seconds: float = Field(default=10.0, gt=0)
    beep_cache_ttl: int = Field(default=300, gt=0)
    """质押年化缓存秒数。金库收益按批注入（初期每周 2~3 次），秒级刷新没意义。"""

    @model_validator(mode="after")
    def validate_production(self) -> Self:
        """仅在 production 下检查独立凭据、日志模式、签名域名和明确的跨域来源。

        任一项不合格就拒绝启动；这里不建立数据库连接，也不检测外部服务是否在线。
        """
        if self.environment == "production":
            if (
                not self.foundation_database_url
                or not self.agent_database_url
                or self.foundation_database_url == self.agent_database_url
            ):
                raise ValueError("生产环境必须分别配置 FOUNDATION_DATABASE_URL 与 AGENT_DATABASE_URL")
            if "copilot_dev_rw" in self.database_url:
                raise ValueError("生产环境必须配置独立 DATABASE_URL 凭据")
            if self.debug:
                raise ValueError("生产环境必须设置 DEBUG=false")
            if (
                self.jwt_secret == "dev-only-insecure-secret-change-me-before-deploy"
                or len(self.jwt_secret.strip().encode()) < 32
            ):
                raise ValueError("生产环境 JWT_SECRET 必须使用至少 32 字节的独立随机密钥")
            if self.siwe_domain.split(":")[0] in {"localhost", "127.0.0.1", ""}:
                raise ValueError("生产环境必须配置 SIWE_DOMAIN")
            if not self.cors_origin_list or "*" in self.cors_origin_list:
                raise ValueError("生产环境必须明确配置 CORS_ORIGINS")
        return self

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

    @property
    def cors_origin_list(self) -> list[str]:
        """将逗号分隔的来源配置转换为列表，并去掉空项和首尾空格。"""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """缓存当前进程的配置对象，避免每次请求重复解析 .env。

    运行期修改环境变量不会自动刷新缓存；测试需显式清缓存或注入 Settings。
    """
    return Settings()


settings = get_settings()
