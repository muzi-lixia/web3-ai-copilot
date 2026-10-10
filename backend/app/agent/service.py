"""轻量对话协调：框架负责推理，应用只负责任务、持久化状态和 SSE 展示。

一个 Agent worker，PostgreSQL 租约防止重复进程。轮次幂等、明确排队和终态；
终态写库失败保留待补写项，维护任务重试，避免永久 running / 409。
"""

import asyncio
import time
from contextlib import aclosing
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_core.messages import HumanMessage
from langsmith import tracing_context

from app.agent.context import RunContext
from app.agent.memory import ContextBudget
from app.agent.models import chat_model, select_settings
from app.agent.tools import TOOLS
from app.core.exceptions import AppError, RateLimitError
from app.core.logging import get_logger, span

PROMPT = """你是只读 Web3 查询助手。支持本人余额、已收录代币概览、公开行情和结果汇总。
自然回应“你好”等问候并简要介绍能力；意图由你理解，不把问候当成错误。
用户问题不明确时自然追问，不猜测缺失参数。不支持的交易等请求说明只读范围。
输入中的[地址已隐藏]、[密钥已隐藏]、[金额已隐藏]是隐私占位符，不猜测或还原。
钱包由基础服务绑定，不请求也不填写钱包地址。每次最新余额请求重新调用工具。
用户指定币种则只查该币种，BERA/WBERA 独立。缺币种、缺合约网络或跨轮指代不明确时追问。
历史结果仅能通过 result_id 引用。模型不计算或编造余额、估值，不用公开价格推算持仓。
工具返回的是元数据；真实数据通过结果卡片展示。不可声称工具失败时已经查到余额。
汇总只能调用 aggregate_results，不重复相加。对不支持的交易和投资建议明确拒绝。"""


class ChatService:
    def __init__(self, repository, client, settings, lease, checkpointer):
        self.repository, self.client, self.settings = repository, client, settings
        self.lease, self.checkpointer = lease, checkpointer
        self.tasks = {}
        self.pending = {}
        self.slots = asyncio.Semaphore(settings.model_concurrency)
        self.maintenance = None
        self.stopping = False
        self.last_cleanup = 0
        self.accept_lock = asyncio.Lock()

    async def start(self):
        await self.lease.acquire()
        try:
            async with self.repository.db.sessions.begin() as s:
                from sqlalchemy import text

                await s.execute(text("SELECT agent_recover_turns()"))
        except BaseException:
            await self.lease.close()
            raise
        self.maintenance = asyncio.create_task(self.maintain())

    async def maintain(self):
        """只重试最终状态，不重跑模型或工具；所有等待都有暂停，避免数据库空转。"""
        while True:
            await asyncio.sleep(2)
            try:
                await self.lease.verify()
                if time.monotonic() - self.last_cleanup > 60:
                    from sqlalchemy import text

                    async with self.repository.db.sessions() as session:
                        expired = (
                            await session.execute(text("SELECT * FROM expired_agent_conversations()"))
                        ).all()
                    for row in expired:
                        await self.checkpointer.adelete_thread(row.id)
                        await self.repository.clear_expired(row.user_id, row.id)
                    self.last_cleanup = time.monotonic()
                for tid, (user, values) in list(self.pending.items()):
                    await self.repository.update(user, tid, **values)
                    self.pending.pop(tid, None)
            except Exception:
                get_logger(__name__).exception("turn.recovery_failed")
                if self.lease.failed:
                    for task in self.tasks.values():
                        task.cancel()
                    return

    async def submit(self, user, token, client_id, safe, display_text=None):
        """接受阶段短锁保护总队列；幂等重发不受“队列已满”误拦截。"""
        async with self.accept_lock:
            await self.lease.verify()
            conversation = await self.repository.conversation(user)
            if conversation.get("expired_thread_id"):
                await self.checkpointer.adelete_thread(conversation["expired_thread_id"])
            existing = await self.repository.find_client_turn(
                user, conversation["id"], client_id, safe.text, display_text
            )
            if existing:
                return dict(turn_id=existing, created=False)
            if self.stopping or len(self.tasks) + len(self.pending) >= self.settings.chat_queue_limit:
                raise RateLimitError("等待队列已满，请稍后重试")
            await self.client.request("POST", "/auth/agent-permits", token)
            tid, created = await self.repository.create_turn(
                user, conversation["id"], client_id, safe.text, display_text
            )
            if created:
                task = asyncio.create_task(self.generate(user, conversation["id"], tid, token, safe))
                self.tasks[tid] = task
                task.add_done_callback(lambda done: self.tasks.pop(tid, None))
            return dict(turn_id=tid, created=created)

    async def generate(self, user, cid, tid, token, safe):
        answer, state, error, usage = "", "failed", None, {}
        context = RunContext(user, cid, token, self.client, self.repository)
        with span("turn.completed", turn_id=tid), tracing_context(enabled=False):
            try:
                settings = select_settings(self.settings, await self.repository.model_provider(user, cid))
                async with asyncio.timeout(self.settings.chat_queue_timeout_seconds):
                    await self.slots.acquire()
                try:
                    await self.lease.verify()
                    await self.client.identity(token)
                    await self.repository.update(user, tid, "", "running", [])
                    if safe.clarification:
                        answer = safe.clarification
                    else:
                        async with asyncio.timeout(self.settings.copilot_timeout_seconds):
                            async with chat_model(settings) as model:
                                agent = create_agent(
                                    model,
                                    tools=TOOLS,
                                    system_prompt=PROMPT,
                                    context_schema=RunContext,
                                    checkpointer=self.checkpointer,
                                    middleware=[
                                        ContextBudget(settings),
                                        ModelCallLimitMiddleware(
                                            run_limit=self.settings.agent_model_call_limit,
                                            exit_behavior="error",
                                        ),
                                        ToolCallLimitMiddleware(
                                            run_limit=self.settings.agent_tool_call_limit,
                                            exit_behavior="error",
                                        ),
                                    ],
                                )
                                config = {
                                    "configurable": {"thread_id": cid},
                                    "recursion_limit": self.settings.agent_model_call_limit * 4 + 8,
                                }
                                tick = time.monotonic()
                                async with aclosing(
                                    agent.astream(
                                        {"messages": [HumanMessage(content=safe.text, id=str(uuid4()))]},
                                        context=context,
                                        config=config,
                                        stream_mode="messages",
                                    )
                                ) as source:
                                    async for message, _ in source:
                                        if message.type == "AIMessageChunk" and not message.tool_call_chunks:
                                            answer += str(message.text)
                                            if message.usage_metadata:
                                                usage = {
                                                    "prompt_tokens": message.usage_metadata.get(
                                                        "input_tokens", 0
                                                    ),
                                                    "completion_tokens": message.usage_metadata.get(
                                                        "output_tokens", 0
                                                    ),
                                                }
                                        if time.monotonic() - tick >= self.settings.copilot_snapshot_seconds:
                                            await self.lease.verify()
                                            await self.repository.update(
                                                user, tid, answer, "running", context.results
                                            )
                                            tick = time.monotonic()
                        if not answer.strip():
                            answer = (
                                "查询已完成，请查看数据卡片。"
                                if context.results
                                else "请明确要查询的币种和网络。"
                            )
                    state = "completed"
                finally:
                    self.slots.release()
            except asyncio.CancelledError:
                state, error = "cancelled", "已停止生成"
            except TimeoutError:
                error = "查询等待或执行超时，请重试"
            except AppError as exc:
                error = exc.code
            except Exception:
                error = "model_unavailable"
                get_logger(__name__).exception("turn.failed", extra={"turn_id": tid})
            finally:
                get_logger(__name__).info(
                    "turn.state", extra={"turn_id": tid, "status": state, "error_code": error, **usage}
                )
                values = dict(answer=answer, state=state, results=context.results, error=error, usage=usage)
                try:
                    await self.lease.verify()
                    await self.repository.update(user, tid, **values)
                except Exception:
                    self.pending[tid] = (user, values)
                    get_logger(__name__).exception("turn.persistence_pending", extra={"turn_id": tid})

    async def snapshot(self, user, tid):
        row = await self.repository.read(user, tid)
        # 当前 worker 已结束而终态尚未补写，明确告知客户端，不把它误显示为模型运行中。
        value = self.repository.snapshot(row)
        value["persistence_pending"] = tid in self.pending
        return value

    async def events(self, user, tid, token):
        """每秒读一个持久化快照，断线不取消生成；有变化才发送，超时发心跳。

        不保存 token 事件历史，重连直接返回最新文本和引用，不重复执行工具。
        连接存续期间再次验证登录会话，登出之后停止提供数据。
        """
        version, heartbeat = -1, time.monotonic()
        while True:
            await self.lease.verify()
            await self.client.identity(token)
            value = await self.snapshot(user, tid)
            active = value["status"] in ("queued", "running") or value["persistence_pending"]
            revision = (value["version"], value["persistence_pending"])
            if revision != version:
                kind = "status" if value["status"] == "queued" else "text_delta" if active else "done"
                if value["message"].get("result_refs"):
                    yield dict(type="result_ref", **value)
                yield dict(type=kind, **value)
                version = revision
            if not active:
                return
            if time.monotonic() - heartbeat >= 15:
                yield {"type": "ping"}
                heartbeat = time.monotonic()
            await asyncio.sleep(1)

    async def cancel(self, user, tid):
        row = await self.repository.read(user, tid)
        task = self.tasks.get(tid)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            row = await self.repository.read(user, tid)
            if row["state"] in ("queued", "running") and tid not in self.pending:
                await self.repository.update(
                    user, tid, row["answer"], "cancelled", row["results"], error="已停止生成"
                )
        return await self.snapshot(user, tid)

    async def clear(self, user, token=None):
        """短接受锁防止清空与同用户提交交错；先删除真实结果，再删除对话引用。"""
        async with self.accept_lock:
            history = await self.repository.history(user)
            if history["active_turn_id"]:
                await self.cancel(user, history["active_turn_id"])
            conversation = await self.repository.conversation(user)
            refs = await self.repository.refs(user, conversation["id"])
            if token:
                for offset in range(0, len(refs), 10):
                    await self.client.request(
                        "DELETE",
                        "/me/balance-results",
                        token,
                        json={"result_ids": refs[offset : offset + 10]},
                    )
            await self.checkpointer.adelete_thread(conversation["id"])
            await self.repository.clear(user)
            for tid, (owner, _) in list(self.pending.items()):
                if owner == user:
                    self.pending.pop(tid, None)

    async def stop(self):
        self.stopping = True
        if self.maintenance:
            self.maintenance.cancel()
            await asyncio.gather(self.maintenance, return_exceptions=True)
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.lease.close()
