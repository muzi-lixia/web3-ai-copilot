"""通过运行中的两个 HTTP 服务进行真实模型冒烟；凭证不打印、不写日志。

先启动 PostgreSQL、基础服务、Agent 和 Ollama，再运行本脚本。
脚本只使用随机测试钱包，结束后撤销登录，不读取现有用户的数据。
"""

import asyncio
import json
from uuid import uuid4

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct


async def main():
    account = Account.create()
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000/api/v1", timeout=30) as base:
        challenge = (await base.post("/auth/challenges", json={"address": account.address})).json()["data"]
        signature = "0x" + account.sign_message(encode_defunct(text=challenge["message"])).signature.hex()
        login = await base.post(
            "/auth/tokens", json={"message": challenge["message"], "signature": signature}
        )
        login.raise_for_status()
        credentials = login.json()["data"]
        headers = {"Authorization": "Bearer " + credentials["token"]}
        try:
            async with httpx.AsyncClient(
                base_url="http://127.0.0.1:8001/api/v1", headers=headers, timeout=200
            ) as agent:
                opened = await agent.put("/chat/session")
                opened.raise_for_status()
                accepted = await agent.post(
                    "/chat/session/turns",
                    json={"message": "查询我的 BERA 余额", "client_msg_id": str(uuid4())},
                )
                accepted.raise_for_status()
                tid = accepted.json()["data"]["turn_id"]
                async with agent.stream("GET", f"/chat/turns/{tid}/events") as stream:
                    stream.raise_for_status()
                    async for line in stream.aiter_lines():
                        if line.startswith("data: "):
                            value = json.loads(line[6:])
                            if value["type"] == "error":
                                raise RuntimeError(value["code"])
                            if value["type"] == "done":
                                if value["status"] != "completed" or not value["message"]["result_refs"]:
                                    raise RuntimeError("模型没有完成真实资产工具调用")
                                print("真实模型、资产工具、结果引用、SSE：通过。")
                                print("trace_id：" + stream.headers.get("x-trace-id", ""))
                await agent.delete("/chat/data")
        finally:
            await base.delete("/auth/session", headers=headers)


if __name__ == "__main__":
    asyncio.run(main())
