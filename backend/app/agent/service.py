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
    """对话运行时的唯一协调者：受理、排队、生成、落库与订阅都在这里收口。

    一致性模型：数据库状态、内存任务登记、部署租约三者可能对不上，因此启动时收尾
    遗留轮次，终态写库失败进入待补写队列，租约失效则整体停摆 —— 目标是不留下
    「永久 running」的会话，也不让同一次生成被两个进程同时写。
    """

    def __init__(self, repository, client, settings, lease, checkpointer):
        """装配运行时依赖：tasks 管活动任务、pending 管待补写终态、slots 限制模型并发。"""
        self.repository, self.client, self.settings = repository, client, settings
        self.lease, self.checkpointer = lease, checkpointer
        # 活动生成任务：tid -> Task。任务结束（含异常）由 done_callback 摘除。
        self.tasks = {}
        # 终态写库失败的轮次：tid -> (user, 终态字段)。由维护任务重试，是「不留下永久 running」的兜底。
        self.pending = {}
        # 模型并发额度。本机单模型时为 1；超出额度的轮次在这里排队，而不是立刻失败。
        self.slots = asyncio.Semaphore(settings.model_concurrency)
        self.maintenance = None
        self.stopping = False
        self.last_cleanup = 0
        # 只保护「受理」临界区（查重 + 建轮次 + 登记任务），不覆盖生成过程。
        self.accept_lock = asyncio.Lock()

    async def start(self):
        """接部署租约，并收尾上次进程遗留的 queued/running 轮次。

        收尾必须在拿到租约之后：否则两个进程可能同时「恢复」同一批轮次。
        """
        await self.lease.acquire()
        try:
            async with self.repository.db.sessions.begin() as s:
                from sqlalchemy import text

                await s.execute(text("SELECT agent_recover_turns()"))
        except BaseException:
            await self.lease.close()
            raise
        # 维护任务与租约同生共死：租约失效时它会自行退出（见 maintain）。
        self.maintenance = asyncio.create_task(self.maintain())

    async def maintain(self):
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
                    self.last_cleanup = time.monotonic()
                # 补写只重放终态字段，不重跑模型或工具——这里要的只是「把结果存进去」。
                # 用 list(...) 做快照迭代，允许循环内 pop 而不改变被迭代对象。
                for tid, (user, values) in list(self.pending.items()):
                    await self.repository.update(user, tid, **values)
                    self.pending.pop(tid, None)
            except Exception:
                get_logger(__name__).exception("turn.recovery_failed")
                # 租约已被判定失败：取消在跑任务并永久退出维护，绝不自动重抢（防双写）。
                if self.lease.failed:
                    for task in self.tasks.values():
                        task.cancel()
                    return

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
            if self.stopping or len(self.tasks) + len(self.pending) >= self.settings.chat_queue_limit:
                raise RateLimitError("等待队列已满，请稍后重试")
            # 建轮次之前，先向基础服务确认该用户仍被允许使用 Agent。
            await self.client.request("POST", "/auth/agent-permits", token)
            tid, created = await self.repository.create_turn(
                user, conversation["id"], client_id, safe.text, display_text
            )
            if created:
                # 生成脱离 HTTP 请求生命周期运行；done_callback 只摘除任务登记，
                # 终态由 generate 自己负责写入（写失败则进 pending）。
                task = asyncio.create_task(self.generate(user, conversation["id"], tid, token, safe))
                self.tasks[tid] = task
                task.add_done_callback(lambda done: self.tasks.pop(tid, None))
            return dict(turn_id=tid, created=created)

    async def generate(self, user, cid, tid, token, safe):
        """后台生成一轮回答；脱离 HTTP 生命周期，终态必须落库或进入待补写。

        状态机：排队等模型额度 → running → 终态。无论走哪条分支，finally 都要写出
        终态；写失败就登记待补写交给维护任务重试，不能让订阅端永远等一个不会到来的结果。
        """
        # 默认终态是 failed：任何没有显式置为 completed 的路径都不会被误当成成功。
        answer, state, error, usage = "", "failed", None, {}
        context = RunContext(user, cid, token, self.client, self.repository)
        with span("turn.completed", turn_id=tid), tracing_context(enabled=False):
            try:
                settings = select_settings(self.settings, await self.repository.model_provider(user, cid))
                # 排队等模型额度（不是执行超时）：等不到就抛 TimeoutError，走下面的兜底分支。
                async with asyncio.timeout(self.settings.chat_queue_timeout_seconds):
                    await self.slots.acquire()
                try:
                    # 顺序固定：先确认租约与凭证仍然有效，再把轮次从 queued 推进到 running。
                    await self.lease.verify()
                    await self.client.identity(token)
                    await self.repository.update(user, tid, "", "running", [])
                    if safe.clarification:
                        # 已在受理阶段判定不该进模型（如代币合约缺网络），直接当作最终回答，不消耗模型额度。
                        answer = safe.clarification
                    else:
                        # 整个 Agent 执行（含多轮工具调用）共享这一个超时；超时统一走 TimeoutError。
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
                                # aclosing 保证循环提前退出（超时/异常）时关闭异步生成器，释放底层连接。
                                async with aclosing(
                                    agent.astream(
                                        {"messages": [HumanMessage(content=safe.text, id=str(uuid4()))]},
                                        context=context,
                                        config=config,
                                        stream_mode="messages",
                                    )
                                ) as source:
                                    async for message, _ in source:
                                        # 只累计纯文本块：带 tool_call_chunks 的块里是「边说边调工具」的内容，
                                        # 计入正文会把工具调用片段混进回答。
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
                                        # 落库节流：每 snapshot_seconds 才写一次全量正文，
                                        # 避免每个 token 都触发一次 UPDATE。
                                        if time.monotonic() - tick >= self.settings.copilot_snapshot_seconds:
                                            # 长回答期间周期性复查租约，避免失去租约后仍在写库。
                                            await self.lease.verify()
                                            await self.repository.update(
                                                user, tid, answer, "running", context.results
                                            )
                                            tick = time.monotonic()
                        # 模型没产出正文时给一句确定的兜底，绝不展示空回答：
                        # 已经有结果卡片就说已完成，否则提示补参数。
                        if not answer.strip():
                            answer = (
                                "查询已完成，请查看数据卡片。"
                                if context.results
                                else "请明确要查询的币种和网络。"
                            )
                    state = "completed"
                finally:
                    # 无论成功失败都要归还模型额度，否则后续轮次会永久排队。
                    self.slots.release()
            except asyncio.CancelledError:
                # 主动取消是正常终态，不按异常记录。
                state, error = "cancelled", "已停止生成"
            except TimeoutError:
                # 排队超时与执行超时共用这一分支：对用户而言都是「等太久」，不必区分。
                error = "查询等待或执行超时，请重试"
            except AppError as exc:
                # 业务错误直接暴露错误码；文案由统一错误处理器映射，避免这里重复维护。
                error = exc.code
            except Exception:
                # 兜底（多为模型服务不可用）：对外只给安全错误码，详情仅进日志。
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
                    # 唯一终态出口：正常/失败/取消都汇聚到这里。写库失败不能吞掉——
                    # 登记待补写交给维护任务重试，否则订阅端会永远等一个不会到来的终态。
                    self.pending[tid] = (user, values)
                    get_logger(__name__).exception("turn.persistence_pending", extra={"turn_id": tid})

    async def snapshot(self, user, tid):
        """读取一轮的展示快照，并标注终态是否尚未补写成功。"""
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
        # version 从 -1 起步：保证第一帧一定发出，哪怕库里 version 恰好是 0。
        version, heartbeat = -1, time.monotonic()
        while True:
            # 每次推送前复查租约与登录会话：登出或换实例后立即停止供数。
            await self.lease.verify()
            await self.client.identity(token)
            value = await self.snapshot(user, tid)
            active = value["status"] in ("queued", "running") or value["persistence_pending"]
            # revision 把 version 与待补写标志打包：补写完成（标志翻转）也算一次变化，需要再推一帧。
            revision = (value["version"], value["persistence_pending"])
            if revision != version:
                kind = "status" if value["status"] == "queued" else "text_delta" if active else "done"
                if value["message"].get("result_refs"):
                    # 有结果引用时额外发一帧，前端据此把数据卡片挂到对应回答上。
                    yield dict(type="result_ref", **value)
                # 事件名沿用三态语义，但 payload 始终是全量快照（不是增量），前端按整条替换。
                yield dict(type=kind, **value)
                version = revision
            if not active:
                # 已是终态且无待补写：推完最后一帧就结束流，客户端不必自己判断何时断开。
                return
            if time.monotonic() - heartbeat >= 15:
                # 15s 心跳：保活中间代理，也让前端能区分「安静」和「连接已死」。
                yield {"type": "ping"}
                heartbeat = time.monotonic()
            # 轮询间隔与落库节流同量级，避免无意义的空读。
            await asyncio.sleep(1)

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
            if row["state"] in ("queued", "running") and tid not in self.pending:
                await self.repository.update(
                    user, tid, row["answer"], "cancelled", row["results"], error="已停止生成"
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
            for tid, (owner, _) in list(self.pending.items()):
                if owner == user:
                    self.pending.pop(tid, None)

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
