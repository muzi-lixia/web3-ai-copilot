"""单轮 Agent 执行：排队、模型与工具调用、流式快照和终态提交。"""

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
from app.agent.knowledge_policy import RequireKnowledge
from app.agent.llm import chat_models, resolve_session_provider, select_settings, without_thinking
from app.agent.middleware import ContextBudget, LiveDataFilter
from app.agent.rag import rewrite as rewrite_query
from app.agent.tools import TOOLS
from app.core.exceptions import AppError, KnowledgeRetrievalError
from app.core.logging import get_logger, log_context, span, trace_id

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
     检索的 query 要脱离对话也能独立看懂：出现「它」「这个」这类指代时，先换成对话里出现过
     的具体名字再传。
5. 正文用自然语言说明本次结果：查到什么、金额多少、数据截止时间。所有金额、数量、时间
   必须逐字引用工具返回值，不四舍五入、不换算货币、不自行计算；工具没返回的数字不要补。
   用知识库片段作答时，在相应句子后标出片段编号（如 [1]、[1][2]），并说明出自哪份文档；
   编号必须与检索结果里的编号一致，不把片段当成自己的话，也不引用没有编号的内容。
6. 只有本轮成功执行 search_docs 且返回 no_answer 才能说明未找到依据。检索失败、索引不兼容
   或服务不可用都不代表没有资料，禁止声称已经查询或知识库不支持该主题。
   检索返回 no_answer 时，明确说明知识库未找到依据，不用自身知识补答、不编造引用。
   回答知识库问题时只答被问到的内容：检索片段是参考资料，不逐条总结，与问题无关的功能、
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


class TurnExecutor:
    def __init__(self, repository, client, settings, lease, checkpointer, streams, recovery):
        self.repository, self.client, self.settings = repository, client, settings
        self.lease, self.checkpointer = lease, checkpointer
        self.streams, self.recovery = streams, recovery
        self.slots = asyncio.Semaphore(settings.model_concurrency)

    async def run(self, user, cid, tid, permit, safe):
        """后台生成一轮回答；脱离 HTTP 生命周期，终态必须落库或进入待补写。

        permit 是受理时换来的委托凭证，整轮取数都用它：用户令牌在生成期间过期，
        不再让这一轮作废。

        状态机：排队等模型额度 → running → 终态。无论走哪条分支，finally 都要写出
        终态；写失败就登记待补写交给维护任务重试，不能让订阅端永远等一个不会到来的结果。
        """
        # 默认终态是 failed：任何没有显式置为 completed 的路径都不会被误当成成功。
        started = time.monotonic()
        queue_done = first_token = None
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
            self.streams.broadcast(tid, None)

        context.persist = persist_results
        with log_context(turn_id=tid, run_id=tid), span("turn.completed", turn_id=tid) as outcome:
            try:
                provider = await self.repository.model_provider(user, cid)
                settings = select_settings(self.settings, resolve_session_provider(self.settings, provider))
                # 排队等模型额度（不是执行超时）：等不到就抛 TimeoutError，走下面的兜底分支。
                async with asyncio.timeout(self.settings.chat_queue_timeout_seconds):
                    await self.slots.acquire()
                queue_done = time.monotonic()
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
                                        RequireKnowledge(),
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
                                    "run_id": tid,
                                    "metadata": {"turn_id": tid, "session_id": cid,
                                                 "trace_id": trace_id.get(), "run_id": tid},
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
                                                if message.text and first_token is None:
                                                    first_token = time.monotonic()
                                                answer += str(message.text)
                                                # 增量推送：把累计正文广播给本进程的订阅者，
                                                # 速度由 stream_seconds 决定，与落库节流无关。
                                                if time.monotonic() - push >= stream_seconds:
                                                    self.streams.broadcast(tid, answer)
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
                state, error = "cancelled", "turn_cancelled"
            except TimeoutError:
                # 排队超时与执行超时共用这一分支：对用户而言都是「等太久」，不必区分。
                error = "queue_timeout" if queue_done is None else "execution_timeout"
            except KnowledgeRetrievalError as exc:
                # 失败不是 no_answer，丢弃未完成正文，界面按明确错误码展示故障。
                answer, error = "", exc.code
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
                outcome.update(status="success" if state == "completed" else state, error_code=error)
                ended = time.monotonic()
                timings = {
                    "queue_ms": round(((queue_done or ended) - started) * 1000, 2),
                    "execution_ms": round((ended - queue_done) * 1000, 2) if queue_done else 0,
                    "first_token_ms": round((first_token - started) * 1000, 2) if first_token else None,
                    "total_ms": round((ended - started) * 1000, 2),
                }
                usage = (
                    dict(
                        prompt_tokens=sum(prompt for prompt, _ in usage_by_call.values()),
                        completion_tokens=sum(completion for _, completion in usage_by_call.values()),
                    )
                    if usage_by_call
                    else {}
                )
                get_logger(__name__).info(
                    "turn.state",
                    extra={
                        "turn_id": tid,
                        "status": state,
                        "error_code": error,
                        **usage,
                        **timings,
                        "pending_count": len(self.recovery.pending),
                    },
                )
                values = dict(
                    answer=answer,
                    state=state,
                    results=context.results,
                    error=error,
                    usage=usage,
                    tool_trace=context.tool_calls,
                    timings=timings,
                )
                await self.recovery.persist_terminal(user, tid, values)
                # 终态已定：补发最后一段正文，再通知订阅者立即重读 —— 不必等下一个 1 秒刻度。
                self.streams.broadcast(tid, answer)
                self.streams.broadcast(tid, None)
