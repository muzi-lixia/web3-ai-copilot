"""单实例对话运行时：后台生成、正文快照、取消与独立订阅。

生成任务属于轮次，不属于某个 HTTP 请求：刷新、切页或断线只结束订阅，不取消模型生成。
内存快照负责低延迟展示，数据库快照负责重启恢复；正常结束、失败和取消立即保存终态。
数据库 advisory lock 强制单实例/单 worker，确保启动时能安全收尾遗留运行。
这是进程内任务调度器，不提供进程崩溃后的模型推理续跑能力。
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from app.ai.conversation.repository import ChatRepository
from app.ai.llm.protocol import ChatModel
from app.ai.memory.service import ConversationMemory
from app.core.config import Settings
from app.core.exceptions import AppError, ChatUnavailableError, ConflictError, NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class RuntimeLease(Protocol):
    """调度只依赖租约能力，PostgreSQL 实现在装配层绑定。"""

    failed: bool

    async def acquire(self) -> None:
        """取得当前运行时独占租约；已有实例占用时必须拒绝启动。"""
        ...

    async def verify(self) -> None:
        """确认租约仍有效，未启动或租约丢失必须抛异常而非自动继续写入。"""
        ...

    async def close(self) -> None:
        """释放租约持有的专用连接；不负责生成任务与业务数据清理。"""
        ...


@dataclass
class Running:
    """某个活动轮次的进程内状态；条件变量通知所有订阅者，而不是把结果绑定给单一连接。"""

    owner: str
    session_id: UUID
    snapshot: dict
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task | None = None


@dataclass
class PendingTerminal:
    """终态暂存：数据库恢复后补写原目标状态，不能仅丢掉任务并留下永久 running。"""

    state: Running
    content: str
    status: str
    error: str | None
    usage: dict | None


class ChatRuntime:
    """管理单实例内的生成、摘要和订阅；数据库是持久化真源，内存只保存正在运行的状态。"""

    def __init__(
        self,
        repository: ChatRepository,
        memory: ConversationMemory,
        model: ChatModel,
        lock: RuntimeLease,
        settings: Settings,
    ) -> None:
        """初始化任务注册表、带引用计数的用户锁以及共享的单模型并发额度。"""
        # 只保存活动轮次；结束后移除，之后的查询与重连从数据库读取已提交终态。
        self.running: dict[UUID, Running] = {}
        self.summaries: dict[UUID, asyncio.Task] = {}
        self.locks: dict[str, tuple[asyncio.Lock, int]] = {}
        # 本机 Ollama 的前台回答与摘要共用一个额度，避免同时推理挤占内存和算力。
        self.model_slots = asyncio.Semaphore(1)
        self.pending_terminals: dict[UUID, PendingTerminal] = {}
        # 查询仅登记待维护对象，业务写入由独立恢复任务执行。
        self.recovery_candidates: dict[UUID, str] = {}
        self.recovery_wakeup = asyncio.Event()
        self.recovery_task: asyncio.Task | None = None
        self.stopping = False
        self.repository = repository
        self.memory = memory
        self.model = model
        self.lock = lock
        self.settings = settings

    @asynccontextmanager
    async def owner_lock(self, owner: str) -> AsyncIterator[None]:
        """串行化同一用户的提交与删除，避免删会话时同时接受新问题；引用归零后移除锁以回收内存。"""
        key = owner.lower()
        lock, count = self.locks.get(key, (asyncio.Lock(), 0))
        self.locks[key] = (lock, count + 1)
        try:
            async with lock:
                yield
        finally:
            _, count = self.locks[key]
            if count == 1:
                del self.locks[key]
            else:
                self.locks[key] = (lock, count - 1)

    async def start(self) -> None:
        """先取得租约，再恢复历史，最后启动补写任务；失败时立即释放租约。"""
        self.stopping = False
        try:
            await self.lock.acquire()
            await self.repository.recover_interrupted()
        except BaseException:
            self.stopping = True
            await self.lock.close()
            raise
        self.recovery_task = asyncio.create_task(self.recover_pending())

    async def verify_lease(self) -> None:
        """检查单实例写入资格，租约失效时停止调度并取消其他生成与摘要任务。

        跳过当前任务自身，避免在错误处理过程中自我取消；后续写入均拒绝，
        不能重新抢锁后让旧任务覆盖新实例恢复的数据。
        """
        try:
            await self.lock.verify()
        except ChatUnavailableError:
            self.stopping = True
            current = asyncio.current_task()
            tasks = [state.task for state in self.running.values()] + list(self.summaries.values())
            for task in tasks:
                if task and task is not current:
                    task.cancel()
            raise

    async def stop(self) -> None:
        """停止生成与摘要任务并等待其收尾，随后释放运行锁和连接池。"""
        self.stopping = True
        tasks = [state.task for state in self.running.values()] + list(self.summaries.values())
        for task in tasks:
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task), return_exceptions=True)
        # 尚未进入 generate 的任务没有执行 finally，关闭时也应回收登记状态。
        self.running.clear()
        if self.recovery_task:
            self.recovery_task.cancel()
            await asyncio.gather(self.recovery_task, return_exceptions=True)
            self.recovery_task = None
        # 退出前再尝试一次；仍不可写时由下一次启动恢复数据库中的遗留状态。
        await self.flush_pending()
        await self.lock.close()

    async def session(self, owner: str) -> dict:
        """每个用户只开放一个固定会话；同一用户的首次并发打开只创建一次。"""
        async with self.owner_lock(owner):
            await self.verify_lease()
            if self.stopping:
                raise ChatUnavailableError("聊天服务正在停止")
            return await self.repository.get_or_create_session(owner)

    async def _submit_locked(
        self,
        owner: str,
        session_id: UUID,
        client_id: UUID,
        question: str | None = None,
        retry_of: UUID | None = None,
    ) -> dict:
        """调用方已持用户锁，固定会话解析、幂等接受和任务登记不能被删除操作穿插。"""
        # 接受新请求前先收尾数据库中的孤儿 running，否则它会一直占用唯一活动轮次。
        active = await self.repository.active_turn(owner, session_id)
        if active and not self.has_live_worker(active):
            await self.reconcile(owner, active)
        turn_id, created = await self.repository.create_turn(owner, session_id, client_id, question, retry_of)
        if created:
            snapshot = await self.repository.turn_snapshot(owner, turn_id)
            state = Running(owner.lower(), session_id, snapshot)
            # 先登记内存状态再启动任务，订阅和取消才能观察到同一个轮次对象。
            self.running[turn_id] = state
            state.task = asyncio.create_task(self.generate(turn_id, state))
            state.task.add_done_callback(self.observe_task)
        return {"turn_id": str(turn_id), "created": created}

    async def history(self, owner: str, before_seq: int | None = None, limit: int = 50) -> dict:
        """在用户锁内查找固定会话并读取历史页，不隐式创建资源。

        用户锁防止“刚选出会话就被删除”的竞态；仓储的读快照保证分页正文、
        活动轮次和摘要版本来自同一数据库视图。
        """
        async with self.owner_lock(owner):
            await self.verify_lease()
            current = await self.repository.find_session(owner)
            if current is None:
                return {"items": [], "next_cursor": None, "active_turn_id": None, "summary_version": None}
            page = await self.repository.messages(owner, UUID(current["id"]), before_seq, limit)
            if page["active_turn_id"] and not self.has_live_worker(UUID(page["active_turn_id"])):
                self.queue_recovery(owner, UUID(page["active_turn_id"]))
            return page

    async def submit_message(self, owner: str, client_id: UUID, question: str) -> dict:
        """新问题的公开业务入口：验证租约、解析唯一会话，再原子接受并登记后台生成。

        整段操作共用用户锁，避免清空数据与首次创建、提交任务交错。返回的是
        接受结果而非模型正文；网络重发复用 client_id，主动发送新问题使用新 ID。
        """
        async with self.owner_lock(owner):
            await self.verify_lease()
            if self.stopping:
                raise ChatUnavailableError("聊天服务正在停止")
            current = await self.repository.get_or_create_session(owner)
            return await self._submit_locked(owner, UUID(current["id"]), client_id, question)

    async def retry(self, owner: str, turn_id: UUID, client_id: UUID) -> dict:
        """验证原轮次归属及固定会话关系，再创建一次新的生成尝试。

        原问题由服务器读取，客户端不能修改；最后轮次及可重试终态由仓储进一步
        检查。新 client_id 表示主动重试，同一个重试请求的网络重发仍复用该 ID。
        """
        async with self.owner_lock(owner):
            await self.verify_lease()
            if self.stopping:
                raise ChatUnavailableError("聊天服务正在停止")
            original = await self.repository.turn_snapshot(owner, turn_id)
            current = await self.repository.get_or_create_session(owner)
            if original["session_id"] != current["id"]:
                raise ConflictError("只能重试当前固定会话的最后一轮")
            return await self._submit_locked(owner, UUID(current["id"]), client_id, retry_of=turn_id)

    @staticmethod
    def observe_task(task: asyncio.Task) -> None:
        """读取后台任务异常，避免失败被当作无人处理的任务异常；数据库故障需要保留可排查日志。"""
        if not task.cancelled() and task.exception() is not None:
            error = task.exception()
            logger.error("Chat task failed", exc_info=(type(error), error, error.__traceback__))

    async def publish(
        self,
        state: Running,
        content: str,
        status: str = "running",
        error: str | None = None,
        usage: dict | None = None,
    ) -> None:
        """发布进程内完整正文快照并递增版本；唤醒所有订阅者，慢连接可跳过中间快照。"""
        # 修改版本与唤醒订阅者放在同一条件锁内，避免观察到新正文却沿用旧版本。
        async with state.changed:
            state.snapshot = {
                **state.snapshot,
                "version": state.snapshot["version"] + 1,
                "status": status,
                "error": error,
            }
            state.snapshot["message"] = {
                **state.snapshot["message"],
                "content": content,
                "status": "streaming" if status == "running" else status,
            }
            if usage is not None:
                state.snapshot["usage"] = usage
            state.changed.notify_all()

    async def persist(
        self,
        turn_id: UUID,
        state: Running,
        content: str,
        status: str = "running",
        error: str | None = None,
        usage: dict | None = None,
    ) -> None:
        """先写入数据库，再发布对应快照；终态也经过此入口，确保已宣布结束的正文已经持久化。"""
        await self.verify_lease()
        result = await self.repository.checkpoint(
            state.owner, turn_id, content, status, error, usage, version=state.snapshot["version"] + 1
        )
        if result is None:
            raise NotFoundError("轮次已删除或结束")
        await self.publish(state, content, status, error, usage)

    def has_live_worker(self, turn_id: UUID) -> bool:
        """同时检查登记状态、任务存在性和完成标记。

        数据库 running 只说明最后持久化状态，不能证明后台任务仍在工作。
        """
        state = self.running.get(turn_id)
        return state is not None and state.task is not None and not state.task.done()

    async def finalize(
        self,
        turn_id: UUID,
        state: Running,
        content: str,
        status: str,
        error: str | None = None,
        usage: dict | None = None,
    ) -> bool:
        """保存终态失败时留下补写记录并唤醒订阅者，避免任务异常退出后无人收尾。"""
        try:
            await self.persist(turn_id, state, content, status, error, usage)
        except ChatUnavailableError as exc:
            await self.publish(state, content, "interrupted", exc.message)
            return False
        except NotFoundError:
            self.pending_terminals.pop(turn_id, None)
            return False
        except Exception:
            logger.exception("Terminal persistence deferred: %s", turn_id)
            # 保存真正希望写入的终态，不能用向用户展示的临时 failed 替代成功结果。
            self.pending_terminals[turn_id] = PendingTerminal(state, content, status, error, usage)
            self.recovery_wakeup.set()
            # 不能把未保存的成功回答宣布 completed；明确通知用户保存故障，而不是持续等待。
            await self.publish(state, content, "failed", "回答状态保存失败，正在恢复，请稍后刷新会话")
            state.snapshot["persistence_pending"] = True
            return False
        self.pending_terminals.pop(turn_id, None)
        return True

    async def reconcile(self, owner: str, turn_id: UUID) -> dict:
        """在用户锁保护下收尾无活任务的 running；单实例运行锁是这个判断的前提。"""
        await self.verify_lease()
        persisted = await self.repository.turn_snapshot(owner, turn_id)
        if self.has_live_worker(turn_id):
            return self.running[turn_id].snapshot
        pending = self.pending_terminals.get(turn_id)
        if pending is not None:
            if pending.state.owner != owner.lower():
                raise NotFoundError("轮次不存在")
            if persisted["status"] == "running":
                await self.repository.checkpoint(
                    owner,
                    turn_id,
                    pending.content,
                    pending.status,
                    pending.error,
                    pending.usage,
                    version=pending.state.snapshot["version"] + 1,
                )
            self.pending_terminals.pop(turn_id, None)
            if pending.status in {"completed", "truncated"}:
                self.schedule_summary(owner, pending.state.session_id)
        elif persisted["status"] == "running":
            # 覆盖提交已落库但任务未登记、或任务意外退出的情况；不再高频查库空转。
            await self.repository.checkpoint(
                owner,
                turn_id,
                persisted["message"]["content"],
                "interrupted",
                "生成任务已结束，状态未保存，请重试",
            )
        else:
            return persisted
        return await self.repository.turn_snapshot(owner, turn_id)

    def queue_recovery(self, owner: str, turn_id: UUID) -> None:
        """读取发现孤儿状态时仅通知后台维护，不在 GET 调用栈执行业务写入。"""
        self.recovery_candidates[turn_id] = owner.lower()
        self.recovery_wakeup.set()

    async def flush_pending(self) -> None:
        """统一维护孤儿轮次与待补写终态，每个用户的维护与提交使用同一把锁。"""
        if self.lock.failed:
            return
        candidates = dict(self.recovery_candidates)
        candidates.update({key: value.state.owner for key, value in self.pending_terminals.items()})
        for turn_id, owner in candidates.items():
            try:
                async with self.owner_lock(owner):
                    await self.reconcile(owner, turn_id)
                self.recovery_candidates.pop(turn_id, None)
            except NotFoundError:
                self.pending_terminals.pop(turn_id, None)
                self.recovery_candidates.pop(turn_id, None)
            except Exception:
                logger.debug("Terminal persistence still unavailable: %s", turn_id)

    async def recover_pending(self) -> None:
        """通知唤醒或每两秒执行恢复；失败后保留记录且等待，避免数据库故障时忙循环。"""
        while True:
            try:
                await asyncio.wait_for(self.recovery_wakeup.wait(), timeout=2)
            except TimeoutError:
                pass
            self.recovery_wakeup.clear()
            try:
                await self.verify_lease()
            except ChatUnavailableError:
                return
            await self.flush_pending()

    async def generate(self, turn_id: UUID, state: Running) -> None:
        """执行一轮生成，超时包含模型排队、上下文准备和回答生成。

        低延迟展示约每 50ms 更新内存，持久化按配置的快照周期更新；结束立即写入终态。
        取消保留已生成正文，关闭上游流；成功后可安排低优先级摘要。
        """
        # answer 是累计正文；内存发布和数据库检查点都覆盖同一条 assistant 消息。
        answer = ""
        terminal = "failed"
        saved_terminal = False
        try:
            async with asyncio.timeout(self.settings.copilot_timeout_seconds):
                # 优先服务用户对话，先取消可重新生成的后台摘要；请求前的必要压缩仍在本任务中完成。
                for task in list(self.summaries.values()):
                    task.cancel()
                async with self.model_slots:
                    question = await self.repository.prompt_for(state.owner, turn_id)
                    saved = state.snapshot["context_info"]
                    # 失败重试复用原尝试的模型输入，避免新的摘要或历史改变这次重试的语境。
                    # 仅重试已记录过上下文的尝试才复用输入；从未开始的尝试重新构造上下文。
                    if saved.get("model_messages"):
                        messages = saved["model_messages"]
                    else:
                        messages, summary_version, info = await self.memory.build_context(
                            state.owner, state.session_id, question
                        )
                        await self.verify_lease()
                        await self.repository.set_context(state.owner, turn_id, info, summary_version)
                        state.snapshot = {
                            **state.snapshot,
                            "context_info": info,
                            "summary_version": summary_version,
                        }
                    checkpoint_at = emit_at = time.monotonic()
                    # 无论正常结束、异常还是取消，都显式关闭异步生成器，回收 Ollama HTTP 连接。
                    async with aclosing(self.model.stream_chat(messages)) as source:
                        async for event in source:
                            if event["type"] == "delta":
                                answer += event["text"]
                                now = time.monotonic()
                                if now - checkpoint_at >= self.settings.copilot_snapshot_seconds:
                                    await self.persist(turn_id, state, answer)
                                    checkpoint_at = emit_at = now
                                # 展示可以比数据库保存更频繁；崩溃恢复以最近已提交快照为准。
                                elif now - emit_at >= 0.05:
                                    await self.publish(state, answer)
                                    emit_at = now
                            elif event["type"] == "done":
                                terminal = "truncated" if event["partial"] else "completed"
                                saved_terminal = await self.finalize(
                                    turn_id, state, answer, terminal, usage=event.get("usage", {})
                                )
                                break
                        else:
                            raise RuntimeError("Model stream ended without a terminal event")
        # 用户停止与正常停机都保存已生成的半段正文；它只供展示，不进入后续记忆。
        except asyncio.CancelledError:
            terminal = "cancelled"
            saved_terminal = await self.finalize(turn_id, state, answer, terminal, "已停止生成")
        except Exception as exc:
            error = exc.message if isinstance(exc, AppError) else "生成失败，请重试"
            if isinstance(exc, TimeoutError):
                error = "模型回复超时，请重试"
            if not isinstance(exc, (AppError, TimeoutError)):
                logger.exception("Chat generation failed: %s", turn_id)
            saved_terminal = await self.finalize(turn_id, state, answer, "failed", error)
        finally:
            self.running.pop(turn_id, None)
        if saved_terminal and terminal in {"completed", "truncated"}:
            self.schedule_summary(state.owner, state.session_id)

    def schedule_summary(self, owner: str, session_id: UUID) -> None:
        """同一会话最多登记一个后台摘要任务；结束后清理注册项，但不移除后来登记的新任务。"""
        if self.stopping or session_id in self.summaries:
            return
        task = asyncio.create_task(self.background_summary(owner, session_id))
        self.summaries[session_id] = task
        task.add_done_callback(
            lambda finished: (
                self.summaries.pop(session_id, None) if self.summaries.get(session_id) is finished else None
            )
        )

    async def background_summary(self, owner: str, session_id: UUID) -> None:
        """后台预生成摘要；被前台抢占或模型失败时保留旧摘要，由下一轮请求重新检查。"""
        try:
            async with self.model_slots:
                async with asyncio.timeout(self.settings.copilot_timeout_seconds):
                    await self.memory.compact(owner, session_id)
        except asyncio.CancelledError:
            return
        except NotFoundError:
            return
        except Exception:
            logger.warning("Background summary failed for %s; original history retained", session_id)

    async def read_session(self, owner: str) -> dict:
        """读取本人固定会话；不存在返回 404，不创建任何数据。"""
        async with self.owner_lock(owner):
            await self.verify_lease()
            current = await self.repository.find_session(owner)
            if current is None:
                raise NotFoundError("会话不存在")
            return current

    async def snapshot(self, owner: str, turn_id: UUID) -> dict:
        """查询只读取已提交或内存快照；归属确认后通知后台收尾孤儿轮次。"""
        await self.verify_lease()
        persisted = await self.repository.turn_snapshot(owner, turn_id)
        state = self.running.get(turn_id)
        if state and self.has_live_worker(turn_id):
            return state.snapshot
        pending = self.pending_terminals.get(turn_id)
        if persisted["status"] == "running":
            self.queue_recovery(owner, turn_id)
        return pending.state.snapshot if pending else persisted

    async def subscribe(self, owner: str, turn_id: UUID) -> AsyncIterator[dict]:
        """从完整正文快照开始订阅，版本变化后发送新的完整快照。

        无需回放每个 token；断线期间漏掉的增量由最新正文补齐，前端只需按消息 ID 替换。
        等待更新时释放条件锁，发送心跳也不持锁，防止慢订阅者阻塞生成。
        """
        snapshot = await self.snapshot(owner, turn_id)
        # 每次连接先发完整快照，不依赖客户端记住旧游标；这也覆盖刷新或换设备的场景。
        version = -1
        while True:
            if snapshot["version"] != version:
                yield {"type": "snapshot" if snapshot["status"] == "running" else "done", **snapshot}
                version = snapshot["version"]
            if snapshot["status"] != "running":
                return
            state = self.running.get(turn_id)
            if not self.has_live_worker(turn_id):
                # 查询不写终态，等待恢复任务后重新读取；显式等待避免空转查库。
                self.queue_recovery(owner, turn_id)
                await asyncio.sleep(0.25)
                snapshot = await self.snapshot(owner, turn_id)
                if snapshot["version"] == version:
                    yield {"type": "ping"}
                continue
            ping = False
            async with state.changed:
                try:
                    await asyncio.wait_for(
                        state.changed.wait_for(
                            lambda state=state, version=version: state.snapshot["version"] != version
                        ),
                        timeout=15,
                    )
                except TimeoutError:
                    ping = True
                snapshot = state.snapshot
            # 出条件锁以后才 yield，避免网络背压让发布者无法取得锁。
            if ping:
                yield {"type": "ping"}

    async def cancel(self, owner: str, turn_id: UUID) -> dict:
        """独立取消生成并等待终态；即使任务尚未执行第一行，也会将遗留 running 写成 cancelled。"""
        async with self.owner_lock(owner):
            await self.verify_lease()
            persisted = await self.repository.turn_snapshot(owner, turn_id)
            if persisted["status"] not in {"running", "cancelled"}:
                raise ConflictError("该轮次已结束，不能创建取消请求")
            # 与 submit 的任务登记共用锁；等待锁后重新查任务，不能沿用锁外的旧判断。
            state = self.running.get(turn_id)
            if state and state.task:
                state.task.cancel()
                await asyncio.gather(state.task, return_exceptions=True)
                self.running.pop(turn_id, None)
            if turn_id in self.pending_terminals:
                return await self.reconcile(owner, turn_id)
            snapshot = await self.repository.turn_snapshot(owner, turn_id)
            if snapshot["status"] == "running":
                # 任务刚创建就被取消、尚未进入 generate 时，也必须解除会话忙碌。
                await self.repository.checkpoint(
                    owner, turn_id, snapshot["message"]["content"], "cancelled", "已停止生成"
                )
            saved = await self.repository.turn_snapshot(owner, turn_id)
            if state:
                # 任务未进入 generate 时没有 publish，主动唤醒其他标签页的订阅者。
                async with state.changed:
                    state.snapshot = saved
                    state.changed.notify_all()
            return saved

    async def delete_all(self, owner: str) -> None:
        """锁住本人提交入口，停止本人的活动任务，再删除本人全部会话；其他用户的数据不受影响。"""
        async with self.owner_lock(owner):
            await self.verify_lease()
            tasks = [state.task for state in self.running.values() if state.owner == owner.lower()]
            owned = await self.repository.session_ids(owner)
            tasks += [task for session_id, task in self.summaries.items() if session_id in owned]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.repository.delete_all(owner)
            # 数据成功删除后再回收本人内存记录，不能遗留旧状态继续补写。
            for turn_id, state in list(self.running.items()):
                if state.owner == owner.lower():
                    self.running.pop(turn_id, None)
            for turn_id, pending in list(self.pending_terminals.items()):
                if pending.state.owner == owner.lower():
                    self.pending_terminals.pop(turn_id, None)
            for turn_id, address in list(self.recovery_candidates.items()):
                if address == owner.lower():
                    self.recovery_candidates.pop(turn_id, None)
