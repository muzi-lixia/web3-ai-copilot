"""Beep 客户端：Berachain 官方数据层（BeraHub 的页面数据就出自这里）。

## 为什么是它

本项目此前取行情用 DexScreener + DefiLlama（按「链标识 + 合约地址」查，加币即一行）。
**质押的 APY 这两个源都给不出** —— 它不是某个池子的报价，而是协议把 PoL 激励
回购成 WBERA 注入金库后形成的收益，只有 Berachain 自己算得出来。

Beep 就是 BeraHub 前端调的那个后端（Berachain Event Extraction Pipeline），
所以这里的数就是页面上那个数，不是我们另算的近似值。

## 两条与别处不同的约定

1. **必须带 `X-Client-Id`。** 不缺校验、也不注册，但要送一个稳定的小写标识，
   缺失直接 400。标识写在配置里，不在代码里 —— 它属于部署环境。
2. **422 不是错误。** 收益接口在"这段地址历史算不出精确值"时返回 422
   （如转账流水超出回溯上限）。那是**如实告知不可得**，不是故障，
   所以这一种按 None 返回，不抛 UpstreamError —— 抛了会让整个质押页变成 502，
   而实际上仓位数据一条不差。
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import UpstreamError
from app.core.logging import get_logger

logger = get_logger(__name__)

APY_INTERVALS = ("SIX_HOURS", "TWELVE_HOURS", "EIGHTEEN_HOURS", "ONE_DAY", "SEVEN_DAYS")
"""APY 的口径窗口。它必须是显式参数：6 小时的年化和 7 天的年化是**两个数**，
界面上写"APY 6.4%"而不说窗口，等于把最容易被误读的那个量藏起来。"""

DEFAULT_APY_INTERVAL = "ONE_DAY"


@dataclass(frozen=True, slots=True)
class StakeApy:
    """区间年化。**比值不是百分数** —— 0.0644 表示 6.44%。"""

    apy: float
    interval: str


@dataclass(frozen=True, slots=True)
class StakeEarnings:
    """地址在金库上的收益（累计）。

    三个数分开给，是因为它们回答的是不同问题：
      total       累计赚了多少
      realized    其中已经赎回到手的
      unrealized  其中还押在金库里的浮盈
    合成一个数会让"我赚了多少"的答案取决于有没有赎回，而用户问的通常不是这个。

    单位是**底层资产的最小单位**（这里是 WBERA 的 wei），由
    `current_deposit_rate` 的量级可以印证同一个 1e18 标度。
    """

    current_deposit_rate: int
    """当前汇率：1 份份额值多少底层（1e18 标度）。链上 `convertToAssets(1e18)` 同值。"""

    total: int
    realized: int
    unrealized: int


# interval → (过期时刻, StakeApy)。APY 是全链一个数，不随地址变化。
_apy_cache: dict[str, tuple[float, StakeApy]] = {}


def _headers() -> dict[str, str]:
    """构造 Beep 必需的稳定调用方标识与 JSON 接受头，标识来自部署配置。"""
    return {"accept": "application/json", "X-Client-Id": settings.beep_client_id}


def _client() -> httpx.AsyncClient:
    """创建使用统一超时、代理和请求头的短生命周期客户端，由调用方关闭。"""
    return httpx.AsyncClient(
        base_url=settings.beep_base_url,
        timeout=settings.beep_timeout_seconds,
        proxy=settings.market_proxy or None,
        headers=_headers(),
    )


async def get_stake_apy(interval: str = DEFAULT_APY_INTERVAL) -> StakeApy:
    """取质押年化。失败抛 UpstreamError。

    带缓存：金库收益按批注入（初期每周 2~3 次），秒级刷新拿到的还是同一个数，
    只会白白消耗对方的免费额度。
    """
    if interval not in APY_INTERVALS:
        raise UpstreamError(f"未知的 APY 口径 {interval}，可选：{', '.join(APY_INTERVALS)}")

    cached = _apy_cache.get(interval)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]

    try:
        payload = await _get("/v1/stake/apy", params={"interval": interval})
    except _NotComputable as exc:
        # 年化端点不该返回 422（参数错是 400），真发生了也当成"上游说没有"处理，
        # 而不是让它冒成 500 —— 它是个注解层，不值得把整页打掉。
        raise UpstreamError("Beep 表示该口径的年化当前不可用") from exc

    apy = payload.get("apy")
    if apy is None:
        # 尚未开始计息、或对账未完成。这是"上游说没有"，不是取数失败。
        raise UpstreamError(f"Beep 暂未提供 {interval} 口径的质押年化")
    try:
        result = StakeApy(apy=float(apy), interval=str(payload.get("interval") or interval))
        if not math.isfinite(result.apy):
            raise ValueError("APY 必须是有限数值")
    except (TypeError, ValueError) as exc:
        raise UpstreamError(f"Beep 返回的年化无法解析：{apy!r}") from exc

    _apy_cache[interval] = (time.monotonic() + settings.beep_cache_ttl, result)
    return result


async def get_stake_earnings(vault: str, owner: str) -> StakeEarnings | None:
    """取地址在该金库上的收益。**无法精确计算时返回 None**（见模块头第 2 条）。"""
    try:
        payload = await _get(f"/v1/stake/{vault}/earnings/{owner}")
    except _NotComputable:
        logger.info("Beep 无法精确计算 %s 的收益（历史流水不足），本次留空", owner)
        return None

    try:
        return StakeEarnings(
            current_deposit_rate=int(payload["currentDepositRate"]),
            total=int(payload["earningsTotal"]),
            realized=int(payload["earningsRealized"]),
            unrealized=int(payload["earningsUnrealized"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise UpstreamError(f"Beep 收益响应结构不符合预期：{payload!r}") from exc


class _NotComputable(Exception):
    """内部信号：上游明确表示"这个值算不出来"，不是故障。"""


async def _get(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    """发一次 GET 并返回 JSON 对象。失败一律转成 UpstreamError。"""
    try:
        async with _client() as client:
            response = await client.get(path, params=params)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"Beep 请求失败：{type(exc).__name__}") from exc

    if response.status_code == 422:
        raise _NotComputable()
    if response.status_code != 200:
        raise UpstreamError(f"Beep 返回 {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise UpstreamError("Beep 返回的不是 JSON") from exc
    if not isinstance(payload, dict):
        raise UpstreamError("Beep 返回的 JSON 不是对象")
    return payload


async def get_stake_apy_safe(interval: str = DEFAULT_APY_INTERVAL) -> StakeApy | None:
    """吞掉 UpstreamError 的版本。

    APY 是**注解层**，不是质押仓位的主体 —— 拿不到只该少一个数字，
    不该让"你质押了多少"这个问题的答案也一起消失。与资产接口对行情源的
    处理是同一条口径。
    """
    try:
        return await get_stake_apy(interval)
    except UpstreamError as exc:
        logger.warning("质押年化不可用：%s", exc)
        return None


async def gather_earnings(vaults: list[str], owner: str) -> dict[str, StakeEarnings | None]:
    """并发取多个金库的收益；单个失败只丢它自己。键是金库地址小写。"""
    if not vaults:
        return {}

    async def one(vault: str) -> StakeEarnings | None:
        """单个金库收益失败时记录原因并返回 None，让其他金库的查询正常完成。"""
        try:
            return await get_stake_earnings(vault, owner)
        except UpstreamError as exc:
            logger.warning("金库 %s 的收益不可用：%s", vault, exc)
            return None

    results = await asyncio.gather(*(one(vault) for vault in vaults))
    return {vault.lower(): result for vault, result in zip(vaults, results, strict=True)}
