"""业务资源由应用容器持有，配置和缓存显式传递，不依赖请求隐式上下文。"""

from functools import lru_cache
from typing import Any

from app.core.config import Settings, get_settings


class BusinessResources:
    """应用独占配置和缓存，保持对象身份哈希；不在 repr 中展开配置或凭据。"""

    __slots__ = ("settings", "market_cache", "apy_cache", "good_rpc")

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_cache: dict[int, Any] = {}
        self.apy_cache: dict[str, Any] = {}
        self.good_rpc: dict[int, str] = {}

    def __repr__(self):
        return "BusinessResources()"


@lru_cache
def default_resources() -> BusinessResources:
    """仅供独立脚本与直接函数调用；HTTP 和 Agent 始终传容器资源。"""
    return BusinessResources(get_settings())


def resolve_resources(resources: BusinessResources | None) -> BusinessResources:
    return resources if resources is not None else default_resources()
