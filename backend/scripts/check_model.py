#!/usr/bin/env python
"""实打一次模型出口：连得上吗、模型名对不对、真能出字吗。

    .venv/bin/python scripts/check_model.py             # 测当前 MODEL_PROVIDER
    .venv/bin/python scripts/check_model.py deepseek    # 测指定的（可给多个）

为什么需要它：模型配置的错误几乎全是静默的 —— key 填错只在真正提问时才 401，
本地 Ollama 没起与地址写错是同一个「连接失败」，模型名拼错有的平台会兜底到默认模型。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm.client import PLATFORMS, get_client  # noqa: E402

# 本地小模型冷启动要加载权重，卡紧了会把「慢」误报成「连不上」。
TIMEOUT = 180.0

# 回复上限给足：推理模型会把预算先花在思考上，给太小会拿到空回复。
MAX_TOKENS = 512


async def check(platform: str) -> bool:
    try:
        llm = get_client(platform, request_timeout=TIMEOUT, max_tokens=MAX_TOKENS)
    except ValueError as exc:
        print(f"\n── {platform}\n  [!!] {exc}")
        return False

    print(f"\n── {platform} · {llm.model_name}")
    try:
        # root_async_client 就是底下那个原生 AsyncOpenAI，用来证明连通并核对模型名。
        page = await llm.root_async_client.models.list()
    except Exception as exc:
        print(f"  [!!] 连不上：{type(exc).__name__}: {str(exc)[:140]}")
        return False

    names = sorted(item.id for item in page.data)
    if llm.model_name not in names:
        preview = ", ".join(names[:8]) or "（空）"
        print(f"  [!!] {llm.model_name} 不在可用列表里：{preview}")
        if platform == "ollama":
            print(f"       先拉下来：ollama pull {llm.model_name}")
        return False
    print(f"  [ok] 连通，模型在列表里（共 {len(names)} 个）")

    try:
        resp = await llm.ainvoke([{"role": "user", "content": "用一句话回答：你是什么模型？"}])
    except Exception as exc:
        print(f"  [!!] 对话失败：{type(exc).__name__}: {str(exc)[:140]}")
        return False

    text = resp.content.strip().replace("\n", " ") if isinstance(resp.content, str) else ""
    if text:
        print(f"  [ok] 出字正常：{text[:60]}")
        return True
    # 推理模型会把内容全放进思考字段，content 留空。
    print("  [!!] 回复为空（推理模型可能把内容全放进了思考字段）")
    return False


async def main() -> int:
    wanted = [a.lower() for a in sys.argv[1:]] or [os.getenv("MODEL_PROVIDER") or "ollama"]
    unknown = [k for k in wanted if k not in PLATFORMS]
    if unknown:
        print(f"未知平台 {unknown[0]!r}，可选：{', '.join(PLATFORMS)}")
        return 2

    results = [await check(name) for name in wanted]
    print("\n通过。" if all(results) else "\n有问题。")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
