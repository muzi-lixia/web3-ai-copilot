"""轻量对话协调：框架负责推理，应用只负责任务、持久化状态和 SSE 展示。

一个 Agent worker，PostgreSQL 租约防止重复进程。轮次幂等、明确排队和终态；
终态写库失败保留待补写项，维护任务重试，避免永久 running / 409。
"""

import asyncio
import time
from contextlib import aclosing
from functools import partial
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from langchain_core.messages import HumanMessage

from app.agent.context import RunContext
from app.agent.middleware import ContextBudget, LiveDataFilter
from app.agent.models import chat_models, select_settings, without_thinking
from app.agent.rag import rewrite as rewrite_query
from app.agent.tools import TOOLS
from app.core.exceptions import AppError, NotFoundError, RateLimitError
from app.core.logging import get_logger, span

PROMPT = """你是只读 Web3 助手，回答本人资产、公开行情与平台知识库文档相关的问题。
规则：
1. 所有链上资产、合约数据必须调用工具获取，禁止编造数据。
2. 不能自动执行转账、授权等任何链上交易；仅可讲解操作步骤与风险。
3. 缺省与歧义处理：
   - 调用工具前先检查参数。钱包已由基础服务绑定，不索取也不填写钱包地址。
   - 没有指定币种时，按整体持仓一次查完，不追问币种。
   - 没指定网络时使用默认网络，不追问。
   - 「我有多少钱」「我有多少资产」问的是本人持仓，按整体持仓查询。
   - 「BERA 多少钱」这类没提「我的」才按公开行情理解。
   - BERA 与 WBERA 是不同币种，不混用。
4. 工具选择：先判断题目的答案是数值还是一段说明，再选工具：
   - 数值：「我有多少 X」「我的资产」「我的持仓」用余额工具：用户点到的币种填进它的 symbols，
     一个也没点到就留空 symbols、按整体持仓查；「X 多少钱」用行情工具。
   - 一段说明：问规则、周期、时间表、上限、怎么操作、有什么限制都属于这一类 —— 只要答案
     不是数值、要的是文档里写的依据，就用 search_docs，不要用余额或行情工具顶替。
5. 正文用自然语言说明本次结果：查到什么、金额多少、数据截止时间。所有金额、数量、时间
   必须逐字引用工具返回值，不四舍五入、不换算货币、不自行计算；工具没返回的数字不要补。
   用知识库片段作答时，在相应句子后标出片段编号（如 [1]、[1][2]），并说明出自哪份文档；
   编号必须与检索结果里的编号一致，不把片段当成自己的话，也不引用没有编号的内容。
6. 回答知识库问题时只答被问到的内容：检索片段是参考资料，不逐条总结，与问题无关的功能、
   参数、背景不提；先给直接答案，仅在理解答案必需时补充解释。片段涉及风险、限制或歧义时
   照常说明。
7. 不提供投资建议；拒绝讲解盗币、合约攻击等违规内容。
8. 不诱导用户泄露私钥、助记词。
每次问最新余额或行情都重新调用工具，不复用旧结果；知识库检索同样每问必查，不复用历史片段；
对话历史里出现过的数值不是当前数据；没查过的结论先查再答。
输入的[地址已隐藏]、[密钥已隐藏]、[金额已隐藏]是隐私占位符，不猜测、不还原。
问候自然回应并简述能做什么；超出范围的请求直接说明只读范围，不多解释。"""

# 对用户可见的正文只来自主模型节点：工具内部再发起的模型调用（如检索改写）分片同样会
# 被流式捕获，归在 tools 节点下，不是本轮回答的内容。
ANSWER_NODE = "model"


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
        # 本进程内的订阅者：tid -> 等待新正文的队列。生成与订阅同进程才有效，
        # 当前部署是单进程（--workers 1 + 租约）所以成立；换多进程后这里会静默没人收到，
        # 订阅端仍有数据库基线兜底，不会丢结果。
        self.live = {}

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
            # 换证：用用户的访问令牌换一张覆盖整轮的委托凭证，后台生成全程用它取数。
            # 这一步同时是身份校验（令牌无效会在这里 401），且发生在建轮次之前 ——
            # 建了轮次再失败，用户就白等一轮。
            permit = await self.client.request("POST", "/auth/agent-permits", token)
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
        """后台生成一轮回答；脱离 HTTP 生命周期，终态必须落库或进入待补写。

        permit 是受理时换来的委托凭证，整轮取数都用它：用户令牌在生成期间过期，
        不再让这一轮作废。

        状态机：排队等模型额度 → running → 终态。无论走哪条分支，finally 都要写出
        终态；写失败就登记待补写交给维护任务重试，不能让订阅端永远等一个不会到来的结果。
        """
        # 默认终态是 failed：任何没有显式置为 completed 的路径都不会被误当成成功。
        answer, state, error = "", "failed", None
        # 按模型调用 id 收集用量：一次调用的分片共享 id，只在收尾分片带用量，
        # 同 id 取最后一次即为该次总量，多轮工具调用不会被最后一次覆盖。
        usage_by_call: dict[str | None, tuple[int, int]] = {}
        context = RunContext(user, cid, permit, self.client, self.repository)

        async def persist_results():
            """工具产出结果后立即落库，并唤醒订阅端重读 —— 卡片不必等 1 秒刻度。

            正文传当前累计值：update 是整列覆盖，传空串会把已经流出的正文抹掉。
            顺序固定为「先落库（事务提交）→ 再广播」，否则通知先到、数据还没提交。
            """
            try:
                await self.repository.update(
                    user, tid, answer, "running", context.results, tool_trace=context.tool_calls
                )
            except Exception:
                # 即时落库只是把刻度提前，失败不能拖垮这一轮：
                # 1 秒刻度与终态写库仍会兜住，必要时还有待补写队列。
                get_logger(__name__).exception("turn.persist_early_failed", extra={"turn_id": tid})
                return
            self.broadcast(tid, None)

        context.persist = persist_results
        with span("turn.completed", turn_id=tid):
            try:
                settings = select_settings(self.settings, await self.repository.model_provider(user, cid))
                # 排队等模型额度（不是执行超时）：等不到就抛 TimeoutError，走下面的兜底分支。
                async with asyncio.timeout(self.settings.chat_queue_timeout_seconds):
                    await self.slots.acquire()
                try:
                    # 顺序固定：先确认租约与凭证仍然有效，再把轮次从 queued 推进到 running。
                    await self.lease.verify()
                    await self.client.identity(permit)
                    await self.repository.update(user, tid, "", "running", [])
                    if safe.clarification:
                        # 已在受理阶段判定不该进模型（如代币合约缺网络），直接当作最终回答，不消耗模型额度。
                        answer = safe.clarification
                    else:
                        # 整个 Agent 执行（含多轮工具调用）共享这一个超时；超时统一走 TimeoutError。
                        async with asyncio.timeout(self.settings.copilot_timeout_seconds):
                            async with chat_models(settings) as (model, rewriter, rewrite_conf):
                                # 改写默认与 Agent 共用本轮模型实例；配置了独立提供方时用它自己的。
                                # 改写不需要思考链，单独关掉换回往返时间。
                                context.rewrite = partial(
                                    rewrite_query, without_thinking(rewriter, rewrite_conf)
                                )
                                agent = create_agent(
                                    model,
                                    tools=TOOLS,
                                    system_prompt=PROMPT,
                                    context_schema=RunContext,
                                    checkpointer=self.checkpointer,
                                    middleware=[
                                        LiveDataFilter(),
                                        ContextBudget(settings),
                                        # 步数上限用 "error"：撞上限直接抛错中止，绝不静默收尾。
                                        # 抛出的两个 *ExceededError 在下面单独映射成 agent_step_limit，
                                        # 不会和 model_unavailable 混为一谈。
                                        # 试过 "end"（收尾返回已有结果），但正文可能只写半句却显示
                                        # 「完成」，坏得无声、排查无从下手，故改回 "error"。
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
                                    # 上报到 LangSmith 时的标识：按轮次命名并带上会话与轮次 id，
                                    # 前端给出的那一轮可以直接在平台上按 id 搜到。
                                    "run_name": "copilot.turn",
                                    "metadata": {"turn_id": tid, "session_id": cid},
                                }
                                tick = push = time.monotonic()
                                # 增量推送节拍：比落库节流细得多，又不用每个 token 都发一帧。
                                stream_seconds = self.settings.copilot_stream_seconds
                                # aclosing 保证循环提前退出（超时/异常）时关闭异步生成器，释放底层连接。
                                async with aclosing(
                                    agent.astream(
                                        {"messages": [HumanMessage(content=safe.text, id=str(uuid4()))]},
                                        context=context,
                                        config=config,
                                        stream_mode="messages",
                                    )
                                ) as source:
                                    async for message, metadata in source:
                                        if message.type == "AIMessageChunk":
                                            # 用量与正文无关：每次模型调用的收尾分片带用量，
                                            # 即使这一轮是去调工具也要记，否则多轮用量会漏掉前几步。
                                            if message.usage_metadata:
                                                usage_by_call[message.id] = (
                                                    message.usage_metadata.get("input_tokens", 0),
                                                    message.usage_metadata.get("output_tokens", 0),
                                                )
                                            # 只累计主模型节点的纯文本块：带 tool_call_chunks 的块是
                                            # 「边说边调工具」的内容，计入会把调用片段混进回答；
                                            # 工具内部发起的模型调用分片归 tools 节点，不是正文。
                                            node = (metadata or {}).get("langgraph_node")
                                            if not message.tool_call_chunks and node == ANSWER_NODE:
                                                answer += str(message.text)
                                                # 增量推送：把累计正文广播给本进程的订阅者，
                                                # 速度由 stream_seconds 决定，与落库节流无关。
                                                if time.monotonic() - push >= stream_seconds:
                                                    self.broadcast(tid, answer)
                                                    push = time.monotonic()
                                        # 落库节流：每 snapshot_seconds 才写一次全量正文，
                                        # 避免每个 token 都触发一次 UPDATE。
                                        if time.monotonic() - tick >= self.settings.copilot_snapshot_seconds:
                                            # 长回答期间周期性复查租约，避免失去租约后仍在写库。
                                            await self.lease.verify()
                                            await self.repository.update(
                                                user,
                                                tid,
                                                answer,
                                                "running",
                                                context.results,
                                                tool_trace=context.tool_calls,
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
            except (ModelCallLimitExceededError, ToolCallLimitExceededError):
                # 撞步数上限是额度耗尽，不是模型故障：单独给码，不落进下面的兜底。
                error = "agent_step_limit"
            except Exception:
                # 兜底（多为模型服务不可用）：对外只给安全错误码，详情仅进日志。
                error = "model_unavailable"
                get_logger(__name__).exception("turn.failed", extra={"turn_id": tid})
            finally:
                usage = (
                    dict(
                        prompt_tokens=sum(prompt for prompt, _ in usage_by_call.values()),
                        completion_tokens=sum(completion for _, completion in usage_by_call.values()),
                    )
                    if usage_by_call
                    else {}
                )
                get_logger(__name__).info(
                    "turn.state", extra={"turn_id": tid, "status": state, "error_code": error, **usage}
                )
                values = dict(
                    answer=answer,
                    state=state,
                    results=context.results,
                    error=error,
                    usage=usage,
                    tool_trace=context.tool_calls,
                )
                try:
                    await self.lease.verify()
                    await self.repository.update(user, tid, **values)
                except Exception:
                    # 唯一终态出口：正常/失败/取消都汇聚到这里。写库失败不能吞掉——
                    # 登记待补写交给维护任务重试，否则订阅端会永远等一个不会到来的终态。
                    self.pending[tid] = (user, values)
                    get_logger(__name__).exception("turn.persistence_pending", extra={"turn_id": tid})
                # 终态已定：补发最后一段正文，再通知订阅者立即重读 —— 不必等下一个 1 秒刻度。
                self.broadcast(tid, answer)
                self.broadcast(tid, None)

    async def authorize(self, user, tid):
        """订阅前的归属校验；不拥有就按不存在处理，不暴露他人轮次是否存在。"""
        if not await self.repository.owns(user, tid):
            raise NotFoundError("轮次不存在")

    async def snapshot(self, user, tid):
        """读取一轮的展示快照，并标注终态是否尚未补写成功。"""
        row = await self.repository.read(user, tid)
        # 当前 worker 已结束而终态尚未补写，明确告知客户端，不把它误显示为模型运行中。
        value = self.repository.snapshot(row)
        value["persistence_pending"] = tid in self.pending
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
