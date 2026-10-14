"""扫历史轮次的工具调用轨迹，看线上真实发生过的调用形态。

只读 `agent_turns.tool_trace`，不调模型、不取链上数据、不写任何表。

要看的是首轮路由层测不到的那部分：同一轮里把同一个工具调两次（看到结果不满意
再试一次）、一轮调了五六次。那些是多轮行为，只有在真实轮次的轨迹里才看得到。

轮次带行级安全，不设身份一行都读不到，所以地址是必填的（写成库内小写形态）。

用法（在 backend 目录下）：
    .venv/bin/python -m scripts.inspect_tool_calls <用户地址> [最近轮数，默认 50]
"""

import asyncio
import json
import sys
from collections import Counter

from sqlalchemy import text

from app.core.config import get_settings
from app.infrastructure.database.session import Database

# 不传轮数时看最近这么多轮。
DEFAULT_LIMIT = 50


def _when(value) -> str:
    """把库里的时间转成本地时区的可读串。"""
    return value.astimezone().strftime("%Y-%m-%d %H:%M") if value else "（无时间）"


def _args(call: dict) -> str:
    """参数的稳定形态：键排序，同一种参数的调用看起来才一致。"""
    return json.dumps(call.get("args") or {}, ensure_ascii=False, sort_keys=True)


def _calls(row: dict) -> list[dict]:
    """一轮的调用列表；轨迹为空时返回空列表而不是 None。"""
    return row["tool_trace"] or []


async def fetch(address: str, limit: int) -> list[dict]:
    """以该地址的身份取最近 limit 轮的轨迹；走应用的同一套事务，不提升权限。"""
    settings = get_settings()
    database = Database(settings.agent_database_url or settings.database_url)
    try:
        async with database.transaction(address) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT created_at, left(query, 32) AS query, tool_trace "
                            "FROM agent_turns ORDER BY created_at DESC LIMIT :n"
                        ),
                        {"n": limit},
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await database.close()
    return [dict(row) for row in rows]


def _repeats(rows: list[dict]) -> list[tuple[dict, str, str, int]]:
    """找出同一轮里重复出现的 (工具, 参数)。

    重复通常是「第一次没拿到想要的东西又试了一次」，一次失败等于多花一整轮模型往返。
    按参数一起比：同一工具传不同参数是并行查多个对象，不算重复。
    """
    found = []
    for row in rows:
        seen = Counter((call.get("tool"), _args(call)) for call in _calls(row))
        for (tool, args), times in seen.items():
            if times > 1:
                found.append((row, tool or "?", args, times))
    return found


def report(rows: list[dict]) -> None:
    """打印调用数分布、多调用的轮次、重复调用与非正常状态的调用。"""
    if not rows:
        print("agent_turns 里没有轮次")
        return
    calls = sum(len(_calls(row)) for row in rows)
    print(f"最近 {len(rows)} 轮 / 共 {calls} 次调用，平均每轮 {calls / len(rows):.1f} 次\n")

    counts = Counter(len(_calls(row)) for row in rows)
    print("每轮调用数")
    for number in sorted(counts):
        print(f"   {number} 次  {counts[number]} 轮")

    # 调用数大于 1 的轮次逐条列出来：一轮里同时问了几件事，一眼可见。
    noisy = [row for row in rows if len(_calls(row)) > 1]
    if noisy:
        print("\n调用数大于 1 的轮次")
        for row in noisy:
            print(f"   {_when(row['created_at'])}  {len(_calls(row))} 次  「{row['query']}」")
            for call in _calls(row):
                print(f"      {call.get('tool')}  {_args(call)}")

    repeats = _repeats(rows)
    print(f"\n同轮重复调用（同一工具同一参数）：{len(repeats)} 处")
    for row, tool, args, times in repeats:
        print(f"   {_when(row['created_at'])}  {tool} ×{times}  {args}")

    abnormal = [
        (row, call) for row in rows for call in _calls(row) if call.get("status") not in (None, "ok")
    ]
    print(f"\n非正常状态的调用：{len(abnormal)} 次")
    for row, call in abnormal:
        print(f"   {_when(row['created_at'])}  {call.get('tool')}  {call.get('status')}")


async def main(address: str, limit: int) -> None:
    """取数据并打印；这里不计算任何指标，只把形态摆出来。"""
    report(await fetch(address, limit))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法：python -m scripts.inspect_tool_calls <用户地址> [最近轮数]")
    asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_LIMIT))
