"""L1 路由层：这一轮该调哪个工具，模型选得对不对。

只发一次模型调用看它挑了什么工具，**不执行工具** —— 不碰数据库、不碰用户凭证、
不取链上数据，也不建图、不带 checkpoint。这一层只依赖主模型可用。

跟随部署配置的主模型（`select_settings(settings, None)`）：这一层要回答的是
「线上那个模型选得对不对」，所以层内不固定提供方，headline 里报出这次实际跑的模型。
会话里按用户选择切换模型的场景不在这里覆盖，评估用的始终是部署默认的那一个。

每条用例有两种结果：
- `ok`   断言满足；
- `fail` 跑完了但断言不满足，是否算「已知缺口」由用例上的标记决定。
"""

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.agent.models import chat_model, select_settings
from app.agent.service import PROMPT
from app.agent.tools import TOOLS
from app.core.config import get_settings
from evals.cases import RoutingCase, load_routing_cases
from evals.report import Report

# 两种判定结果。用字符串常量而不是布尔值，是为了与另外两层共用同一套报告口径。
OK, FAIL = "ok", "fail"

# 注册表里的工具名；用例里写了别的名字会在跑之前报错，不表现成一条用例未通过。
TOOL_NAMES = {tool.name for tool in TOOLS}


def _messages(case: RoutingCase) -> list[BaseMessage]:
    """把用例拼成喂给模型的消息：系统提示词 + 历史 + 本轮问题。

    系统提示词必须带上：路由判据就写在提示词的工具选择那一条里，少了它，
    这一层量的就不是线上那个选择。本轮问题放最后一条，与线上首轮同形。
    """
    messages: list[BaseMessage] = [SystemMessage(content=PROMPT)]
    messages += [
        HumanMessage(content=content) if role == "user" else AIMessage(content=content)
        for role, content in case.history
    ]
    messages.append(HumanMessage(content=case.query))
    return messages


def _matches(case: RoutingCase, calls: list[str]) -> bool:
    """判定一条用例：看首轮实际调用的工具名有没有越过期望与禁区。

    三条期望都在这里生效，任何一条不满足即未通过：
    - `expect_no_tools` 时只要调了工具就算错；
    - `expect_tools` 要求至少命中一个；
    - `forbid_tools` 命中任意一个就算错。
    """
    if case.expect_no_tools and calls:
        return False
    if case.expect_tools and not set(calls) & set(case.expect_tools):
        return False
    if case.forbid_tools and set(calls) & set(case.forbid_tools):
        return False
    return True


def check_names(cases: list[RoutingCase]) -> None:
    """校验用例里写的工具名都在注册表里，写错在跑之前就报出来。

    放在这里而不是用例加载处：只有这一层认识工具注册表，读用例的模块只把它当数据。
    """
    for case in cases:
        unknown = (set(case.expect_tools) | set(case.forbid_tools)) - TOOL_NAMES
        if unknown:
            raise ValueError(f"用例 {case.name} 里的工具名不存在：{'、'.join(sorted(unknown))}")


async def _evaluate(cases: list[RoutingCase], model) -> list[dict]:
    """逐条问模型一次并判定；结果按用例顺序返回，报告里的明细也按这个顺序排。

    `model` 是已经绑好工具的那个实例；这里只读回复里的工具调用，不往下执行。
    """
    outcomes: list[dict] = []
    for case in cases:
        reply = await model.ainvoke(_messages(case))
        calls = [call["name"] for call in getattr(reply, "tool_calls", None) or []]
        outcomes.append(
            {"case": case, "status": OK if _matches(case, calls) else FAIL, "calls": calls}
        )
    return outcomes


def _called(calls: list[str]) -> str:
    """把首轮实际调用的工具排成一行；没调工具时显式写出来，不留空白。"""
    return "、".join(calls) if calls else "（没有调用工具）"


def _expected(case: RoutingCase) -> str:
    """把用例的期望拼成一句人话，与 `_matches` 的三条判据一一对应。"""
    parts: list[str] = []
    if case.expect_no_tools:
        parts.append("不调用任何工具")
    if case.expect_tools:
        parts.append("至少调用 " + "、".join(case.expect_tools) + " 之一")
    if case.forbid_tools:
        parts.append("不调用 " + "、".join(case.forbid_tools))
    return "；".join(parts)


def _problems(outcomes: list[dict]) -> list[str]:
    """把未通过、已知缺口与已修复的缺口各排一段。

    未通过的要给出实际调用的工具：判断是选错了类别，还是压根没调，看这一行就够了。
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
            lines.append(f"        实际 {_called(item['calls'])}")
            lines.append(f"        期望 {_expected(case)}")
    if gaps:
        lines.append(f"\n  已知缺口 {len(gaps)} 条（不计入失败）：")
        for item in gaps:
            case = item["case"]
            lines.append(f"    ○ {case.name}  「{case.query}」")
            # 选不对的原因写在用例文件的 note 里，这里原样带出来。
            if case.note:
                lines.append(f"        {case.note}")
            lines.append(f"        实际 {_called(item['calls'])}")
    if fixed:
        # 缺口被填上了，标记就该撤掉；留着会掩盖真实的退化。
        lines.append("\n  以下用例已能通过，请从用例文件里去掉 expected_fail：")
        lines.extend(f"    • {item['case'].name}" for item in fixed)
    return lines


async def run(service) -> Report:
    """跑完路由层全部用例，返回可比较的结果与可读的失败说明。

    `service` 这一层用不上（路由不查知识库），保留它是为了让各层入口一致。

    名次只有第 1 名与未通过两种取值：这一层没有先后次序，通过就记第 1 名，
    这样能直接沿用同一套基线比对。
    """
    cases = load_routing_cases()
    check_names(cases)
    # provider 传 None：用部署配置的主模型，不固定提供方。
    settings = select_settings(get_settings(), None)
    async with chat_model(settings) as model:
        outcomes = await _evaluate(cases, model.bind_tools(TOOLS))
    passed = [item for item in outcomes if item["status"] == OK]
    return Report(
        key="routing",
        title="路由层 L1",
        headline=f"模型 {settings.model_provider}:{settings.model_name}，用例 {len(cases)} 条",
        passed=len(passed),
        evaluated=len(outcomes),
        # 只有「选错且未标记」的算失败；标记过的已知缺口单独计数。
        failed=len([item for item in outcomes if item["status"] == FAIL and not item["case"].expected_fail]),
        known_gaps=len([item for item in outcomes if item["status"] == FAIL and item["case"].expected_fail]),
        # 明细文字交给本层自己拼，报告模块只负责按行打印。
        problems=_problems(outcomes),
        ranks={item["case"].name: (1 if item["status"] == OK else None) for item in outcomes},
    )
