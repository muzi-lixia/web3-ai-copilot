"""L2 改写层：多轮追问经改写后，能不能脱离上下文独立检索。

只调改写器，不调检索也不调主模型；断言落在改写后的句子上 ——
该补的指代补上了（`expect_any`），不该改的原样返回（`expect_same`）。

改写器本身就是一次模型调用，所以这一层固定跑本地提供方（`PROVIDER`），
不跟随部署配置：它要回答的是「改写这件事，本地模型够不够用」。

每条用例有两种结果：
- `ok`   断言满足；
- `fail` 跑完了但断言不满足，是否算「已知缺口」由用例上的标记决定。
"""

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.agent.config import get_agent_settings
from app.agent.llm import chat_model, select_settings
from app.agent.rag import recent_dialogue, rewriter
from evals.cases import RewriteCase, load_rewrite_cases
from evals.report import Report

# 这一层固定用的提供方：评估为的是量本地模型能不能顶上改写，不看部署配置。
PROVIDER = "ollama"

# 两种判定结果。用字符串常量而不是布尔值，是为了与检索层共用同一套报告口径。
OK, FAIL = "ok", "fail"


def _messages(case: RewriteCase) -> list[BaseMessage]:
    """把用例拼成消息列表：历史在前，本轮问题放最后一条。

    本轮问题必须在末位：改写器按「最后一个用户消息」划出本轮，缺了它，
    历史里最后一条用户消息会被当成本轮，那之前的整段历史都被裁掉。
    """
    messages: list[BaseMessage] = [
        HumanMessage(content=content) if role == "user" else AIMessage(content=content)
        for role, content in case.history
    ]
    messages.append(HumanMessage(content=case.query))
    return messages


def _same(expected: str, actual: str) -> bool:
    """判断改写结果是否与用户原话一致。

    忽略首尾空白与末尾一个标点：模型常在句尾多带一个问号，
    按逐字比会让基线每次都抖。
    """
    return expected.strip().rstrip("？?。.！!") == actual.strip().rstrip("？?。！!")


def _matches(case: RewriteCase, query: str) -> bool:
    """判定一条用例：要求原样返回时比原句，要求补指代时看是否带上了关键词。"""
    if case.expect_same and not _same(case.query, query):
        return False
    if case.expect_any and not any(text in query for text in case.expect_any):
        return False
    return True


async def _warmup(model) -> None:
    """先发一次最短请求，把模型加载进内存。

    首次调用含加载时间（本地 7B 要十几秒），会超过改写那 8 秒预算 ——
    那量的是「模型还没加载」，不是「改写慢」，所以先把它排掉。
    加载失败不在这里拦：用例照跑，失败原因由每条的判定结果暴露。
    """
    try:
        await model.ainvoke("hi")
    except Exception:
        pass


async def _evaluate(cases: list[RewriteCase], model) -> list[dict]:
    """逐条改写并判定；结果按用例顺序返回，报告里的明细也按这个顺序排。"""
    outcomes: list[dict] = []
    for case in cases:
        # 走线上同一条路径：历史按同一个函数裁，改写含同一份超时与失败退回。
        result = await rewriter.rewrite(model, case.query, recent_dialogue(_messages(case)))
        outcomes.append(
            {
                "case": case,
                "status": OK if _matches(case, result.query) else FAIL,
                "query": result.query,
            }
        )
    return outcomes


def _problems(outcomes: list[dict]) -> list[str]:
    """把未通过的、已知缺口、以及已修复的缺口各排一段。

    未通过的要给出改写后的原句：判断是补错了对象、还是被无谓地改写了，
    看这一句就够了。
    """
    failed = [item for item in outcomes if item["status"] == FAIL and not item["case"].expected_fail]
    gaps = [item for item in outcomes if item["status"] == FAIL and item["case"].expected_fail]
    fixed = [item for item in outcomes if item["status"] == OK and item["case"].expected_fail]
    lines: list[str] = []
    if failed:
        lines.append(f"\n  未通过 {len(failed)} 条：")
        for item in failed:
            case = item["case"]
            lines.append(f"    ✗ {case.name}  「{case.query}」")
            lines.append(f"        实际「{item['query']}」")
            # 要求原样返回时把期望写出来，免得被读成「这条用例没有期望」。
            if case.expect_same:
                lines.append("        期望 原样返回")
            else:
                lines.append("        期望 带上 " + "、".join(f'"{text}"' for text in case.expect_any))
    if gaps:
        lines.append(f"\n  已知缺口 {len(gaps)} 条（不计入失败）：")
        for item in gaps:
            case = item["case"]
            lines.append(f"    ○ {case.name}  「{case.query}」")
            # 做不到的原因写在用例文件的 note 里，这里原样带出来。
            if case.note:
                lines.append(f"        {case.note}")
            lines.append(f"        实际「{item['query']}」")
    if fixed:
        # 缺口被填上了，标记就该撤掉；留着会掩盖真实的退化。
        lines.append("\n  以下用例已能通过，请从用例文件里去掉 expected_fail：")
        lines.extend(f"    • {item['case'].name}" for item in fixed)
    return lines


async def run(service) -> Report:
    """跑完改写层全部用例，返回可比较的结果与可读的失败说明。

    `service` 这一层用不上（改写不碰知识库），保留它是为了让各层入口一致。

    headline 报出这次跑的模型，换模型跑时不用回头看配置。
    名次只有第 1 名与未命中两种取值：改写不像检索有先后次序，
    通过就记第 1 名，这样能直接沿用同一套基线比对。
    """
    cases = load_rewrite_cases()
    settings = select_settings(get_agent_settings(), PROVIDER)
    async with chat_model(settings) as model:
        await _warmup(model)
        outcomes = await _evaluate(cases, model)
    passed = [item for item in outcomes if item["status"] == OK]
    changed = [item for item in outcomes if not _same(item["case"].query, item["query"])]
    return Report(
        key="rewrite",
        title="改写层 L2",
        headline=f"模型 {settings.model_provider}:{settings.model_name}，用例 {len(cases)} 条",
        passed=len(passed),
        evaluated=len(outcomes),
        # 只有「未通过的未知缺口问题」算失败；标记过的已知缺口单独计数。
        failed=len([item for item in outcomes if item["status"] == FAIL and not item["case"].expected_fail]),
        known_gaps=len([item for item in outcomes if item["status"] == FAIL and item["case"].expected_fail]),
        # 改写率 = 输出与用户原话不一致的比例；它不判好坏，只在换模型时看倾向。
        metrics={"改写率": len(changed) / len(outcomes) if outcomes else 0.0},
        # 明细文字交给本层自己拼，报告模块只负责按行打印。
        problems=_problems(outcomes),
        ranks={item["case"].name: (1 if item["status"] == OK else None) for item in outcomes},
    )
