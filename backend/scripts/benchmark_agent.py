"""真实 HTTP/SSE 并发压测；需要不同用户的有效凭证，会实际调用模型和工具。

在 backend 目录：python -m scripts.benchmark_agent --tokens-file /tmp/tokens.json
凭证文件是 JSON 字符串数组，不写入仓库；输出仅包含状态计数与延迟，不包含凭证或正文。
"""

import argparse
import asyncio
import json
import math
import time
from pathlib import Path

import httpx


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


async def run_turn(client, token, question):
    from uuid import uuid4

    started, first_text = time.monotonic(), None
    headers = {"Authorization": "Bearer " + token}
    try:
        response = await client.post(
            "/api/v1/chat/session/turns",
            headers=headers,
            json={"message": question, "client_msg_id": str(uuid4())},
        )
        if response.status_code != 202:
            return {"status": f"http_{response.status_code}"}
        turn_id = response.json()["data"]["turn_id"]
        async with client.stream("GET", f"/api/v1/chat/turns/{turn_id}/events", headers=headers) as stream:
            if stream.status_code != 200:
                return {"status": f"http_{stream.status_code}"}
            data = []
            async for line in stream.aiter_lines():
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    event = json.loads("\n".join(data))
                    data = []
                    if event.get("message", {}).get("content") and first_text is None:
                        first_text = (time.monotonic() - started) * 1000
                    if event.get("type") == "error":
                        return {"status": "sse_error"}
                    if event.get("type") == "done":
                        return {
                            "status": event.get("status", "unknown"),
                            "end_to_end_ms": (time.monotonic() - started) * 1000,
                            "first_text_ms": first_text,
                            **event.get("timings", {}),
                        }
        return {"status": "stream_interrupted"}
    except (httpx.HTTPError, ValueError, KeyError):
        return {"status": "request_failed"}


def summarize(results, seconds):
    statuses = {}
    for result in results:
        statuses[result["status"]] = statuses.get(result["status"], 0) + 1
    completed = [r for r in results if r["status"] == "completed"]
    metrics = {}
    for key in ("queue_ms", "execution_ms", "first_token_ms", "first_text_ms", "total_ms", "end_to_end_ms"):
        values = [r[key] for r in completed if r.get(key) is not None]
        metrics[key + "_p95"] = percentile(values, 0.95)
    return {
        "statuses": statuses,
        "completed_per_second": len(completed) / seconds if seconds else 0,
        **metrics,
    }


async def run(args):
    tokens = json.loads(Path(args.tokens_file).read_text())
    if (
        not isinstance(tokens, list)
        or any(not isinstance(t, str) or not t for t in tokens)
        or len(set(tokens)) != len(tokens)
        or len(tokens) < max(args.concurrency)
    ):
        raise ValueError("需要至少等于最大并发数的不同用户凭证；文件内容须为不重复的非空字符串数组")
    async with httpx.AsyncClient(base_url=args.base_url, timeout=300, trust_env=False) as client:
        for count in args.concurrency:
            results, started = [], time.monotonic()
            for _ in range(args.rounds):
                results.extend(
                    await asyncio.gather(
                        *(run_turn(client, token, args.question) for token in tokens[:count])
                    )
                )
            print(json.dumps({"concurrency": count, **summarize(results, time.monotonic() - started)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokens-file", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--question", default="BERA 当前价格是多少")
    args = parser.parse_args()
    if min(args.concurrency) < 1 or args.rounds < 1:
        parser.error("并发数与轮数必须大于零")
    asyncio.run(run(args))
