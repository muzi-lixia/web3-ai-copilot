"""轮次快照、订阅和 SSE 帧；不执行模型或管理恢复任务。"""

import asyncio
import time

from app.core.exceptions import NotFoundError


class TurnStream:
    def __init__(self, repository, client, lease, recovery):
        self.repository, self.client, self.lease = repository, client, lease
        self.recovery = recovery
        self.live = {}

    async def authorize(self, user, tid):
        """订阅前的归属校验；不拥有就按不存在处理，不暴露他人轮次是否存在。"""
        if not await self.repository.owns(user, tid):
            raise NotFoundError("轮次不存在")

    async def snapshot(self, user, tid):
        """读取一轮的展示快照，并标注终态是否尚未补写成功。"""
        row = await self.repository.read(user, tid)
        # 当前 worker 已结束而终态尚未补写，明确告知客户端，不把它误显示为模型运行中。
        value = self.repository.snapshot(row)
        value["persistence_pending"] = tid in self.recovery.pending
        return value

    def broadcast(self, tid, text):
        """把本轮累计正文推给本进程的订阅者；text 为 None 表示库里有新内容。

        只在生成进程与订阅进程相同时送达。换多进程部署后这里会静默地没人收到，
        此时订阅端仍按数据库基线工作（只是退回秒级台阶），不会丢结果也不报错。
        """
        for queue in self.live.get(tid, ()):
            try:
                queue.put_nowait(text)
            except asyncio.QueueFull:
                # 订阅者没跟上：先丢掉它还没读走的旧帧，再放最新一帧。
                # 帧里是累计全量正文，丢中间帧不丢内容。
                queue.get_nowait()
                queue.put_nowait(text)

    @staticmethod
    def frames(value, refs=True):
        """把一份快照翻译成待发送的帧；有结果时补一帧 result_ref。

        事件名沿用三态语义，但 payload 始终是全量快照（不是增量），前端按整条替换。
        refs=False 用于增量帧：结果块没变，不必每帧重复发一遍。
        """
        active = value["status"] in ("queued", "running") or value["persistence_pending"]
        kind = "status" if value["status"] == "queued" else "text_delta" if active else "done"
        frames = [dict(type=kind, **value)]
        if refs and value["message"].get("results"):
            # 有结果时额外发一帧，前端据此把数据卡片挂到对应回答上。
            frames.insert(0, dict(type="result_ref", **value))
        return frames

    async def events(self, user, tid, token):
        """订阅一轮的展示流：数据库基线 + 生成进程内的增量推送，断线不取消生成。

        数据库仍是权威与兜底（断线重连、跨进程、终态都读它），但生成期间不再只靠
        每秒轮询 —— 生成器把累计正文直接广播给本进程的订阅者，消掉 1 秒台阶。
        不保存 token 事件历史，重连直接返回最新文本和引用，不重复执行工具。
        连接存续期间再次验证登录会话，登出之后停止提供数据。
        """
        # 先登记队列再读库：反过来的话，「读库完成 → 登记」之间推送的帧会被丢掉。
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        watchers = self.live.setdefault(tid, set())
        watchers.add(queue)
        # version 从 None 起步：保证第一帧一定发出，哪怕库里还停在初始版本。
        version, heartbeat, read_at = None, time.monotonic(), 0.0
        try:
            while True:
                # 数据库基线按秒读：增量帧不改变库里的 version，不能每来一帧就读一次库。
                # 复查租约与登录会话也放在这一档 —— 它们是远端调用，不能按帧的频率打。
                if time.monotonic() - read_at >= 1:
                    # 登出或换实例后立即停止供数。
                    await self.lease.verify()
                    await self.client.identity(token)
                    value = await self.snapshot(user, tid)
                    # revision 把 version 与待补写标志打包：补写完成（标志翻转）也算一次变化。
                    revision = (value["version"], value["persistence_pending"])
                    if revision != version:
                        for frame in self.frames(value):
                            yield frame
                        version = revision
                    if value["status"] not in ("queued", "running") and not value["persistence_pending"]:
                        # 已是终态且无待补写：推完最后一帧就结束流，客户端不必自己判断何时断开。
                        return
                    read_at = time.monotonic()
                # 等增量帧：拿到就贴进最新快照发出去；超时就回顶部，到点会读库、到点会发心跳。
                try:
                    text = await asyncio.wait_for(queue.get(), timeout=1)
                except TimeoutError:
                    if time.monotonic() - heartbeat >= 15:
                        # 15s 心跳：保活中间代理，也让前端能区分「安静」和「连接已死」。
                        yield {"type": "ping"}
                        heartbeat = time.monotonic()
                    continue
                if text is None:
                    # 终态已落库的信号：立即重读，不必等下一个 1 秒刻度。
                    read_at = 0.0
                    continue
                if text != value["message"]["content"]:
                    live = {**value, "message": {**value["message"], "content": text}}
                    for frame in self.frames(live, refs=False):
                        yield frame
        finally:
            # 订阅结束必须摘除队列，否则生成器会一直往没人读的队列里推、且永不释放。
            watchers.discard(queue)
            if not watchers:
                self.live.pop(tid, None)
