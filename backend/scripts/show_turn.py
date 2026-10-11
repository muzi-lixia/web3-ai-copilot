"""打印某个会话在模型侧的完整消息历史，用于排查「模型为什么这么回答」。

模型实际返回的内容（纯文本还是工具调用）不进 agent_turns，只存在 LangGraph 的
checkpoint 里，所以只能这样读出来。

用法（在 backend 目录下）：
    .venv/bin/python -m scripts.show_turn <conversation_id>

conversation_id 即 agent_turns.conversation_id，也就是 LangGraph 的 thread_id。
"""

import asyncio
import sys

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.engine import make_url

from app.core.config import get_settings


def postgres_dsn(url):
    """把异步 DSN 改成同步驱动名：框架的 PostgresSaver 不接受 asyncpg。"""
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


async def main(thread_id):
    settings = get_settings()
    dsn = postgres_dsn(settings.agent_database_url or settings.database_url)
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        # alist 按时间倒序产出，第一条就是最新那次 checkpoint。
        config = {"configurable": {"thread_id": thread_id}}
        tuples = [tup async for tup in saver.alist(config)]
        if not tuples:
            print(f"没有找到会话 {thread_id} 的模型历史")
            return
        for message in tuples[0].checkpoint["channel_values"].get("messages", []):
            print(f"--- {type(message).__name__} ---")
            # 有 tool_calls 说明模型这一步选择了调工具；没有就是直接给文本。
            tool_calls = getattr(message, "tool_calls", None)
            if tool_calls:
                print("tool_calls:", tool_calls)
            content = message.content
            print(content if isinstance(content, str) else str(content))
            print()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("用法：python -m scripts.show_turn <conversation_id>")
    asyncio.run(main(sys.argv[1]))
