"""只读告警检查；退出码 0 健康，1 达到阈值，2 依赖不可用。由部署监控定时执行。"""

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import text

from app.infrastructure.database.session import Database


def alerts(service, status):
    found = []
    if service == "knowledge":
        if status["workers"] == 0:
            found.append("knowledge_worker_offline")
        if status["oldest_pending_seconds"] > 300:
            found.append("knowledge_queue_stalled")
        if status["failed"] > 0:
            found.append("knowledge_failed_documents")
        if status["cleanup_pending"] > 100:
            found.append("knowledge_cleanup_backlog")
    else:
        if status["oldest_active_seconds"] > 300:
            found.append("agent_turn_stalled")
        if status["total"] >= 5 and status["failed"] / status["total"] >= 0.2:
            found.append("agent_failure_rate")
        if status["checkpoint_pending"] > 100:
            found.append("checkpoint_cleanup_backlog")
        if status["outbox_pending"]:
            found.append("agent_persistence_pending")
    return found


async def check(service):
    if service == "knowledge":
        from app.foundation.config import FoundationSettings
        from app.modules.knowledge.repository import KnowledgeRepository

        settings = FoundationSettings()
        database = Database(settings.foundation_database_url)
        try:
            status = await KnowledgeRepository(database).queue_status()
        finally:
            await database.close()
    else:
        from app.agent.config import AgentSettings

        settings = AgentSettings()
        database = Database(settings.agent_database_url)
        try:
            async with database.sessions() as session:
                status = dict((await session.execute(text(
                    "SELECT * FROM agent_observability_status()"
                ))).mappings().one())
            # 只统计超过 30 秒的补写文件，避免瞬时写前日志造成误报。
            import time

            status["outbox_pending"] = 0
            for path in Path(settings.agent_outbox_dir).glob("*.json"):
                try:
                    status["outbox_pending"] += int(time.time() - path.stat().st_mtime > 30)
                except FileNotFoundError:
                    pass  # 正常提交并删除补写记录，与只读监控并发。
        finally:
            await database.close()
    issues = alerts(service, status)
    print(json.dumps({"service": service, "alerts": issues, "status": status}))
    return int(bool(issues))


async def main(service):
    try:
        return await check(service)
    except Exception as error:
        # 禁止打印可能含 URL、SQL 参数和凭据的异常原文。
        print(json.dumps({"service": service, "alerts": ["monitor_unavailable"],
                          "exception_type": type(error).__name__}))
        return 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=("agent", "knowledge"), required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args().service)))
