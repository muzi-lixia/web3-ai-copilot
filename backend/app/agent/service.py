"""对话协调：受理、幂等、取消与生命周期；执行、推送和恢复由各组件负责。"""

import asyncio
import math
import time

from app.agent.execution import TurnExecutor
from app.agent.recovery import TurnRecovery
from app.agent.streaming import TurnStream
from app.core.exceptions import ChatUnavailableError, RateLimitError
from app.core.logging import get_logger


class ChatService:
    def __init__(self, repository, client, settings, lease, checkpointer):
        self.repository, self.client, self.settings = repository, client, settings
        self.lease, self.checkpointer = lease, checkpointer
        self.tasks = {}
        self.maintenance = None
        self.stopping = False
        self.accept_lock = asyncio.Lock()
        self.recovery = TurnRecovery(repository, lease, checkpointer, settings.agent_outbox_dir)
        self.streams = TurnStream(repository, client, lease, self.recovery)
        self.executor = TurnExecutor(repository, client, settings, lease, checkpointer,
                                     self.streams, self.recovery)

    async def start(self):
        await self.lease.acquire()
        try:
            await self.recovery.restore(self.streams.broadcast)
        except BaseException:
            await self.lease.close()
            raise
        self.maintenance = asyncio.create_task(self.recovery.maintain(self.tasks, self.streams.broadcast))

    async def submit(self, user, token, client_id, safe, display_text=None):
        """接受阶段短锁保护总队列；幂等重发不受“队列已满”误拦截。"""
        # 接受阶段串行化：查重、建轮次、登记任务必须在同一临界区内完成，否则并发提交会重复建轮次。
        async with self.accept_lock:
            # 先验证租约：已失去租约的进程不得再受理新轮次。
            await self.lease.verify()
            conversation = await self.repository.conversation(user)
            if conversation.get("expired_thread_id"):
                await self.checkpointer.adelete_thread(conversation["expired_thread_id"])
            # 幂等回查：同一个 client_msg_id 已经提交过就直接返回原轮次，不再起新任务。
            existing = await self.repository.find_client_turn(
                user, conversation["id"], client_id, safe.text, display_text
            )
            if existing:
                return dict(turn_id=existing, created=False)
            # 容量按「在跑任务 + 待补写」合计：待补写项还没真正落库，但已占用一个未完成的轮次。
            occupied = len(self.tasks) + len(self.recovery.pending)
            if self.stopping or occupied >= self.settings.chat_queue_limit:
                get_logger(__name__).warning(
                    "turn.rejected",
                    extra={
                        "active_turns": len(self.tasks),
                        "pending_count": len(self.recovery.pending),
                        "error_code": "queue_full",
                    },
                )
                raise RateLimitError("等待队列已满，请稍后重试")
            # 换证：用用户的访问令牌换一张覆盖整轮的委托凭证，后台生成全程用它取数。
            # 这一步同时是身份校验（令牌无效会在这里 401），且发生在建轮次之前 ——
            # 建了轮次再失败，用户就白等一轮。
            permit = await self.client.request("POST", "/auth/agent-permits", token)
            expires_at = permit.get("expires_at")
            if (
                isinstance(expires_at, bool)
                or not isinstance(expires_at, (int, float))
                or not math.isfinite(expires_at)
                or expires_at - time.time() < self.settings.required_permit_seconds
            ):
                raise ChatUnavailableError("基础服务签发的委托凭证有效期不足以覆盖排队和执行")
            tid, created = await self.repository.create_turn(
                user, conversation["id"], client_id, safe.text, display_text
            )
            if created:
                # 生成脱离 HTTP 请求生命周期运行；done_callback 只摘除任务登记，
                # 终态由 generate 自己负责写入（写失败则进 pending）。
                task = asyncio.create_task(
                    self.generate(user, conversation["id"], tid, permit["permit"], safe)
                )
                self.tasks[tid] = task
                task.add_done_callback(lambda done: self.tasks.pop(tid, None))
            return dict(turn_id=tid, created=created)

    async def generate(self, user, cid, tid, permit, safe):
        await self.executor.run(user, cid, tid, permit, safe)

    async def authorize(self, user, tid):
        await self.streams.authorize(user, tid)

    async def snapshot(self, user, tid):
        return await self.streams.snapshot(user, tid)

    def events(self, user, tid, token):
        return self.streams.events(user, tid, token)

    async def cancel(self, user, tid):
        """停止一轮生成；没有内存任务（孤儿轮次）时按当前状态返回，不报错。

        取消是幂等的：任务已经结束时再调用不会改变已有终态。
        """
        row = await self.repository.read(user, tid)
        # 只有存在内存任务时才真正取消；孤儿轮次（进程重启后）直接按当前状态返回，不报错。
        task = self.tasks.get(tid)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            row = await self.repository.read(user, tid)
            # 取消后重读：generate 的 finally 若已写出终态就用它的；
            # 只有既没有终态、又没有待补写项时才补写 cancelled，避免覆盖真实结果。
            if row["state"] in ("queued", "running") and tid not in self.recovery.pending:
                await self.repository.update(
                    user, tid, row["answer"], "cancelled", row["results"], error="turn_cancelled"
                )
        # 返回最新快照，接口幂等：重复取消不会改动已有终态。
        return await self.snapshot(user, tid)

    async def clear(self, user, token=None):
        """短接受锁防止清空与同用户提交交错；先删除真实结果，再删除对话引用。"""
        # 持接受锁：清空与同用户的提交必须串行，否则生成任务会把数据写回正在删除的会话。
        async with self.accept_lock:
            history = await self.repository.history(user)
            if history["active_turn_id"]:
                # 运行中删除＝先停后删：先把在跑的轮次取消，避免它继续写库。
                await self.cancel(user, history["active_turn_id"])
            conversation = await self.repository.conversation(user)
            refs = await self.repository.refs(user, conversation["id"])
            if token:
                # 先删基础服务里的真实结果（接口单次上限 10 条，这里分批），再删会话；
                # 顺序反过来会留下没有引用指向的孤儿结果。
                for offset in range(0, len(refs), 10):
                    await self.client.request(
                        "DELETE",
                        "/me/balance-results",
                        token,
                        json={"result_ids": refs[offset : offset + 10]},
                    )
            # 模型历史（checkpoint）与会话行不在同一存储，各删一次。
            await self.checkpointer.adelete_thread(conversation["id"])
            await self.repository.clear(user)
            # 会话已删，本人的待补写项随之作废，防止维护任务对不存在的行空写。
            await self.recovery.discard_user(user)

    async def stop(self):
        """优雅停机：先拒绝新提交，再停维护任务与在跑的生成，最后释放租约。"""
        # 先置停机位拒绝新提交，再停维护任务，最后取消在跑的生成。
        self.stopping = True
        if self.maintenance:
            self.maintenance.cancel()
            await asyncio.gather(self.maintenance, return_exceptions=True)
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # 所有任务都停下后才释放租约：租约一放，别的进程就可能接管并恢复这些轮次。
        await self.lease.close()
