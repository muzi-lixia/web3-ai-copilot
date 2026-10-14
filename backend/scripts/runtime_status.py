"""只读运行状态：队列、worker 心跳和 Agent 最近 24 小时时延，不输出业务数据。"""

import argparse
import asyncio
import json

from sqlalchemy import text

from app.infrastructure.database.session import Database


async def run(service):
    if service == "knowledge":
        from app.foundation.config import FoundationSettings
        from app.modules.knowledge.repository import KnowledgeRepository

        settings = FoundationSettings()
        db = Database(settings.foundation_database_url)
        try:
            print(json.dumps(await KnowledgeRepository(db).queue_status()))
        finally:
            await db.close()
    else:
        from app.agent.config import AgentSettings

        settings = AgentSettings()
        db = Database(settings.agent_database_url)
        try:
            async with db.sessions() as session:
                rows = (await session.execute(text("SELECT * FROM agent_runtime_status()"))).mappings().all()
                print(json.dumps([dict(row) for row in rows]))
        finally:
            await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=("agent", "knowledge"), required=True)
    asyncio.run(run(parser.parse_args().service))
