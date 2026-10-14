"""独立文档进程入口：在 backend 目录运行 python -m app.workers.knowledge。"""

import asyncio
import signal

from app.core.logging import get_logger, service_name, setup_logging
from app.infrastructure.database.session import Database
from app.workers.knowledge.config import WorkerSettings
from app.workers.knowledge.consumer import KnowledgeConsumer

LOGGER = get_logger(__name__)


async def consume(consumer, poll_seconds):
    """单进程顺序处理；数据库异常退避后重试，多进程由任务租约协调。"""
    while True:
        try:
            if await consumer.run_pending():
                continue
        except Exception:
            LOGGER.exception("knowledge.consume_failed")
        await asyncio.sleep(poll_seconds)


async def heartbeat(consumer):
    """独立于长文档处理更新心跳，并输出不含正文的队列统计。"""
    while True:
        try:
            await consumer.repository.heartbeat(consumer.worker_id)
            LOGGER.info("knowledge.queue", extra=await consumer.repository.queue_status())
        except Exception:
            LOGGER.exception("knowledge.heartbeat_failed")
        await asyncio.sleep(10)


async def run_worker(settings, stop):
    """独占数据库连接池；停止时先取消处理和心跳，再释放连接。"""
    database = Database(settings.knowledge_database_url)
    tasks = []
    try:
        try:
            await database.validate_embedding_dimensions(settings.knowledge_embedding_dimensions)
        except Exception as error:
            sqlstate = getattr(error, "sqlstate", None) or getattr(
                getattr(error, "orig", None), "sqlstate", None
            )
            if sqlstate in {"28P01", "28000"}:
                raise RuntimeError(
                    "文档 worker 数据库认证失败：专用账号可能尚未初始化或口令不匹配。"
                    "请检查 KNOWLEDGE_DATABASE_URL；首次运行请在 backend 目录执行 "
                    "uv run python scripts/init_chat_db.py 初始化账号并授权，"
                    "自定义口令须与 KNOWLEDGE_DB_PASSWORD 保持一致。"
                ) from None
            raise
        consumer = KnowledgeConsumer(database, settings)
        LOGGER.info("knowledge.worker_started")
        # 创建两个并发任务
        tasks = [
            # 持续领取文档任务；空闲或失败后等待再重试。
            asyncio.create_task(consume(consumer, settings.knowledge_worker_poll_seconds)),
            # 停机信号监听。当收到停机信号时，stop.wait() 会触发，任务会取消并退出。
            asyncio.create_task(stop.wait()),
            asyncio.create_task(heartbeat(consumer)),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await database.close()
        LOGGER.info("knowledge.worker_stopped")


async def main(settings=None):
    """SIGTERM/SIGINT 触发取消；未完成任务依靠租约过期恢复。"""
    setup_logging()
    token = service_name.set("knowledge-worker")
    # 信号处理器触发事件，run_worker 据此取消任务。
    stop = asyncio.Event()
    # 拿到当前事件循环的引用，后面用它注册信号处理器。
    loop = asyncio.get_running_loop()
    # 记录已注册的信号，用于 finally 里清理。
    registered = []
    try:
        # 收到信号后取消当前任务，租约到期后由其他消费者恢复。
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
            registered.append(signum)
        # 启动 worker 主循环。传入配置和停机事件，worker 会一直跑直到 stop 被触发
        await run_worker(settings or WorkerSettings(), stop)
    finally:
        # 防止进程退出后信号处理器还挂在事件循环上
        for signum in registered:
            loop.remove_signal_handler(signum)
        # 把上下文变量恢复到进入 main 之前的值，避免污染
        service_name.reset(token)
