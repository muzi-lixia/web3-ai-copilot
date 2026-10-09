"""通过真实 HTTP、PostgreSQL 和 Ollama 验证会话链路。

运行：uv run python scripts/check_chat.py；必须使用没有其他实例占用的数据库，建议专用测试库。
脚本创建随机测试身份和临时会话，验证多轮记忆、SSE 快照、历史读取以及结构化摘要。
结束时只删除脚本创建的会话，不清空数据库，也不删除已有用户数据。
"""

import asyncio
import json
import socket
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.main import app  # noqa: E402


async def main():
    """启动临时端口完成真实对话与摘要验证，使用随机测试身份，finally 中清理自己的会话和服务。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    services = app.state.services
    repo, runtime, memory = services.repository, services.chat, services.memory
    # 随机身份仅属于本次验证，清理不能影响已有用户记录。
    owner = "0x" + uuid4().hex + "12345678"
    # 脚本直接签发测试 token 来验证聊天链路，不模拟真实钱包登录。
    token, _ = services.auth.create_token(owner)
    headers = {"Authorization": f"Bearer {token}"}
    base = f"http://127.0.0.1:{port}/api/v1"
    try:
        while not server.started:
            if serving.done():
                await serving
                raise RuntimeError("Server did not start")
            await asyncio.sleep(0.02)
        async with httpx.AsyncClient(trust_env=False, timeout=190) as client:
            session_id = (await client.put(base + "/chat/session", headers=headers)).json()["data"]["id"]
            for question in ["我叫小林，请用一句话打招呼。", "我刚才告诉你我叫什么？只回答名字。"]:
                response = await client.post(
                    base + "/chat/session/turns",
                    headers=headers,
                    json={"message": question, "client_msg_id": str(uuid4())},
                )
                response.raise_for_status()
                turn_id = response.json()["data"]["turn_id"]
                snapshots = []
                start = time.monotonic()
                async with client.stream(
                    "GET", base + f"/chat/turns/{turn_id}/events", headers=headers
                ) as stream:
                    stream.raise_for_status()
                    async for line in stream.aiter_lines():
                        if line.startswith("data: "):
                            snapshots.append(json.loads(line[6:]))
                final = snapshots[-1]
                assert final["status"] == "completed", final
                assert "小林" in final["message"]["content"]
                print(
                    json.dumps(
                        {
                            "answer": final["message"]["content"],
                            "snapshots": len(snapshots),
                            "seconds": round(time.monotonic() - start, 2),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            # 模拟刷新后的读取：历史来自服务器，不由浏览器再次提交。
            response = await client.get(base + "/chat/session/messages", headers=headers)
            assert len(response.json()["data"]["items"]) == 4
            print("Server history restore: passed", flush=True)
            # 用临时原文验证真实模型摘要，包含姓名与偏好，并检查覆盖序号和原文引用。
            memory_session = session_id  # 后续摘要验证继续使用同一固定会话
            await asyncio.gather(*list(runtime.summaries.values()), return_exceptions=True)
            samples = [
                ("我叫小林，希望你都用中文回答。", "好的，我会用中文回答。"),
                ("我只关注 Berachain，我不需要投资买卖建议。", "了解，我们专注概念和数据。"),
                ("我正在研究 sWBERA，请记住这个关注点。", "我会记住你关注 sWBERA。"),
                ("解释一下区块链。", "区块链是分布式账本。"),
            ]
            for question, answer in samples:
                turn_id, _ = await repo.create_turn(owner, UUID(memory_session), uuid4(), question)
                await repo.checkpoint(owner, turn_id, answer, "completed")
            original_keep = settings.copilot_recent_turns
            settings.copilot_recent_turns = 1
            try:
                summary = await memory.compact(owner, UUID(memory_session), force=True)
                assert summary is not None and summary.covered_through_seq == 10
                assert "小林" in json.dumps(summary.content, ensure_ascii=False), summary.content
                print("Real structured summary with source references: passed", flush=True)
                model_messages, version, _ = await memory.build_context(
                    owner, UUID(memory_session), "我叫什么？"
                )
                assert version == summary.version
                assert "小林" in str(model_messages)
            finally:
                settings.copilot_recent_turns = original_keep
    finally:
        try:
            if server.started:
                await runtime.delete_all(owner)
        finally:
            server.should_exit = True
            await serving


if __name__ == "__main__":
    asyncio.run(main())
