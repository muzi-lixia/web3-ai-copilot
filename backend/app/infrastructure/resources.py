"""业务资源由应用容器持有，配置和缓存显式传递，不依赖请求隐式上下文。"""

from functools import lru_cache
from typing import Any

from app.core.settings import EnvSettings
from app.infrastructure.providers.config import ProviderSettings


class ProviderEnvironment(EnvSettings, ProviderSettings):
    """独立脚本的适配器配置，不读取 Foundation 认证、Agent 或数据库设置。"""


class BusinessResources:
    """应用独占配置和缓存，保持对象身份哈希；不在 repr 中展开配置或凭据。"""

    __slots__ = ("settings", "market_cache", "apy_cache", "good_rpc")

    def __init__(self, settings: ProviderSettings):
        """初始化三类进程内缓存：行情、APY、以及按链探测到可用 RPC 的记录。"""
        self.settings = settings
        self.market_cache: dict[int, Any] = {}
        self.apy_cache: dict[str, Any] = {}
        self.good_rpc: dict[int, str] = {}

    def __repr__(self):
        """固定字符串，避免日志或调试输出把配置与缓存整体展开。"""
        return "BusinessResources()"


@lru_cache
def default_resources() -> BusinessResources:
    """仅供独立脚本与直接函数调用；HTTP 和 Agent 始终传容器资源。"""
    return BusinessResources(ProviderEnvironment())


def resolve_resources(resources: BusinessResources | None) -> BusinessResources:
    """把「可选传入」的容器资源归一化：传了就用传的，没传才回落到默认单例。"""
    return resources if resources is not None else default_resources()
