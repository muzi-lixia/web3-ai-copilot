"""终态补写、启动恢复和 checkpoint 清理；不重新执行模型或工具。"""

import asyncio
import time

from sqlalchemy import text

from app.agent.outbox import TerminalOutbox
from app.core.exceptions import ChatUnavailableError
from app.core.logging import get_logger, log_context, span, trace_id


class TurnRecovery:
    def __init__(self, repository, lease, checkpointer, directory):
        self.repository, self.lease, self.checkpointer = repository, lease, checkpointer
        self.outbox = TerminalOutbox(directory)
        self.pending = {}
        self.pending_traces = {}
        self.last_cleanup = 0

    async def restore(self, broadcast):
        await self.outbox.clear_temporary()
        for record in await self.outbox.load():
            tid = record["turn_id"]
            self.pending[tid] = (record["user"], record["values"])
            self.pending_traces[tid] = record["trace_id"]
        await self.flush_pending(broadcast)
        if self.pending:
            raise ChatUnavailableError("待补写终态尚未恢复，请检查持久卷与数据库")
        await self.cleanup_checkpoints()
        async with self.repository.db.sessions.begin() as session:
            await session.execute(text("SELECT agent_recover_turns()"))

    async def cleanup_checkpoints(self):
        for thread_id in await self.repository.checkpoint_deletions():
            try:
                await self.checkpointer.adelete_thread(thread_id)
                await self.repository.finish_checkpoint_deletion(thread_id)
            except Exception:
                get_logger(__name__).exception("checkpoint.cleanup_failed")

    async def flush_pending(self, broadcast):
        for tid, (user, values) in list(self.pending.items()):
            token = trace_id.set(self.pending_traces.get(tid, ""))
            try:
                with log_context(turn_id=tid, run_id=tid), span("turn.persist_recovered"):
                    await self.lease.verify()
                    await self.outbox.save(tid, user, values, trace_id.get())
                    await self.repository.update(user, tid, **values)
                    await self.outbox.remove(tid)
                self.pending.pop(tid, None)
                self.pending_traces.pop(tid, None)
                broadcast(tid, None)
            except Exception:
                get_logger(__name__).exception("turn.recovery_failed", extra={"turn_id": tid})
            finally:
                trace_id.reset(token)

    async def maintain(self, tasks, broadcast):
        """只重试最终状态，不重跑模型或工具；所有等待都有暂停，避免数据库空转。"""
        while True:
            # 固定 2s 节拍：既要尽快补写终态，又不能让空转轮询打满数据库。
            await asyncio.sleep(2)
            try:
                # 租约失效会在这里抛错，跳到 except 结束维护，不再续写（避免与接管的进程双写）。
                await self.lease.verify()
                # 过期会话清理是分钟级粗活，单独节流，避免每个 2s 周期都全表扫一遍。
                if time.monotonic() - self.last_cleanup > 60:
                    from sqlalchemy import text

                    async with self.repository.db.sessions() as session:
                        expired = (
                            await session.execute(text("SELECT * FROM expired_agent_conversations()"))
                        ).all()
                    for row in expired:
                        # 先删模型历史（checkpoint），再删会话行：顺序反了会留下无主的历史。
                        await self.checkpointer.adelete_thread(row.id)
                        await self.repository.clear_expired(row.user_id, row.id)
                    await self.cleanup_checkpoints()
                    get_logger(__name__).info("agent.runtime", extra={
                        "active_turns": len(tasks), "pending_count": len(self.pending),
                        "outbox_pending": len(await self.outbox.load()),
                    })
                    self.last_cleanup = time.monotonic()
                # 补写只重放终态字段，不重跑模型或工具——这里要的只是「把结果存进去」。
                # 用 list(...) 做快照迭代，允许循环内 pop 而不改变被迭代对象。
                await self.flush_pending(broadcast)
            except Exception:
                get_logger(__name__).exception("turn.recovery_failed")
                # 租约已被判定失败：取消在跑任务并永久退出维护，绝不自动重抢（防双写）。
                if self.lease.failed:
                    for task in tasks.values():
                        task.cancel()
                    return

    async def persist_terminal(self, user, tid, values):
        # 先写本地持久卷，再写数据库；数据库提交后才删除补写记录。
        try:
            await self.outbox.save(tid, user, values, trace_id.get())
        except Exception:
            get_logger(__name__).exception("turn.outbox_failed", extra={"turn_id": tid})
        try:
            await self.lease.verify()
            await self.repository.update(user, tid, **values)
            await self.outbox.remove(tid)
        except Exception:
            # 唯一终态出口：正常/失败/取消都汇聚到这里。写库失败不能吞掉——
            # 登记待补写交给维护任务重试，否则订阅端会永远等一个不会到来的终态。
            self.pending[tid] = (user, values)
            self.pending_traces[tid] = trace_id.get()
            get_logger(__name__).exception("turn.persistence_pending", extra={"turn_id": tid})

    async def discard_user(self, user):
        for tid, (owner, _) in list(self.pending.items()):
            if owner == user:
                await self.outbox.remove(tid)
                self.pending.pop(tid, None)
                self.pending_traces.pop(tid, None)
