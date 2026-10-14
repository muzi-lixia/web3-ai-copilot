"""L1 路由层：这一轮该调哪个工具，模型选得对不对。

只发一次模型调用看它挑了什么工具，**不执行工具** —— 不碰数据库、不碰用户凭证、
不取链上数据，也不建图、不带 checkpoint。这一层只依赖主模型可用。

跟随部署配置的主模型（`select_settings(settings, None)`）：这一层要回答的是
「线上那个模型选得对不对」，所以层内不固定提供方，headline 里报出这次实际跑的模型。
会话里按用户选择切换模型的场景不在这里覆盖，评估用的始终是部署默认的那一个。

每条用例的结果落在五种之一：
- `ok`          首轮的选择符合期望；
- `over_call`   多调了：命中禁区、问候却调了工具、或调用条数超过 `max_calls`；
- `under_call`  该调没调：要求调用的工具一个都没命中，且首轮没有调任何工具；
- `wrong_tool`  调了工具，但不是期望的那个；
- `wrong_args`  工具选对了，参数填的不是期望的那组。
后四种是否算「已知缺口」由用例上的标记决定。
"""

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.agent.models import chat_model, select_settings
from app.agent.service import PROMPT
from app.agent.tools import TOOLS
from app.core.config import get_settings
from evals.cases import RoutingCase, load_routing_cases
from evals.report import Report

# 判定结果。用字符串常量而不是布尔值，是为了把几种错法分开成数 ——
# 混成一个「未通过」看不出该往哪个方向收。
OK, OVER, UNDER, WRONG, WRONG_ARGS = "ok", "over_call", "under_call", "wrong_tool", "wrong_args"

# 各种错法的显示名，只用在未通过的明细里。
LABELS = {OVER: "过度调用", UNDER: "调用不足", WRONG: "工具选错", WRONG_ARGS: "参数填错"}

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


def _args_match(case: RoutingCase, calls: list[dict]) -> bool:
    """期望的参数是否填对了：只看期望工具的那一次调用，按子集比对。

    参数省略与显式留空等价 —— 用例写 `symbols = []` 时，模型整条不传也算对，
    因为 schema 的默认值本来就是空。
    """
    call = next((item for item in calls if item["name"] in case.expect_tools), None)
    if call is None:
        return False
    for key, expected in case.expect_args.items():
        actual = call["args"].get(key)
        if actual is None and expected == []:
            continue
        if actual != expected:
            return False
    return True


def _classify(case: RoutingCase, calls: list[dict]) -> str:
    """判定一条用例，返回具体的错法而不是「过没过」。

    判断顺序是有意的：
    - 禁区先判：既没调期望的、又调了禁区的，记「过度调用」更贴切 ——
      它错在调了根本不该调的那个工具，记成「工具选错」会盖住这一点；
    - 问候类要求一个都不调，调了就是过度；
    - 条数超过 `max_calls` 同样是过度：卡的是「多调了一个既不在期望、
      也不在禁区里的工具」，这种只写禁区拦不住；
    - 期望的工具一个都没命中时再看有没有调别的：没调任何工具是「调用不足」，
      调了别的则是「工具选错」；
    - 工具选对了才轮到看参数：填错的与选错的都不是「选对了工具」，但方向不同 ——
      一个要改判据，一个要改参数的取法。
    """
    names = [item["name"] for item in calls]
    if case.forbid_tools and set(names) & set(case.forbid_tools):
        return OVER
    if case.expect_no_tools and calls:
        return OVER
    if case.max_calls is not None and len(calls) > case.max_calls:
        return OVER
    if case.expect_tools and not set(names) & set(case.expect_tools):
        return UNDER if not calls else WRONG
    if case.expect_args and not _args_match(case, calls):
        return WRONG_ARGS
    return OK


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
        # 名字与参数都要：名字判「调了哪个」，参数判「填得对不对」。
        calls = [
            {"name": call["name"], "args": call.get("args") or {}}
            for call in getattr(reply, "tool_calls", None) or []
        ]
        outcomes.append(
            {"case": case, "status": _classify(case, calls), "calls": calls}
        )
    return outcomes


def _called(calls: list[dict]) -> str:
    """把首轮实际调用的工具排成一行；没调工具时显式写出来，不留空白。"""
    return "、".join(item["name"] for item in calls) if calls else "（没有调用工具）"


def _expected(case: RoutingCase) -> str:
    """把用例的期望拼成一句人话，与 `_classify` 的判据一一对应。"""
    parts: list[str] = []
    if case.expect_no_tools:
        parts.append("不调用任何工具")
    if case.expect_tools:
        parts.append("至少调用 " + "、".join(case.expect_tools) + " 之一")
        if case.expect_args:
            filled = "、".join(f"{k}={v!r}" for k, v in case.expect_args.items())
            parts.append(f"且参数为 {filled}")
    if case.forbid_tools:
        parts.append("不调用 " + "、".join(case.forbid_tools))
    return "；".join(parts)


def _problems(outcomes: list[dict]) -> list[str]:
    """把未通过、已知缺口与已修复的缺口各排一段。

    未通过的要给出错在哪一类，以及实际调用了什么：前者决定往哪个方向收，
    后者用来判断是选错了类别，还是压根没调。
    """
    failed = [item for item in outcomes if item["status"] != OK and not item["case"].expected_fail]
    gaps = [item for item in outcomes if item["status"] != OK and item["case"].expected_fail]
    fixed = [item for item in outcomes if item["status"] == OK and item["case"].expected_fail]
    lines: list[str] = []
    if failed:
        lines.append(f"\n  未通过 {len(failed)} 条：")
        for item in failed:
            case = item["case"]
            lines.append(f"    ✗ {case.name}  「{case.query}」  {LABELS[item['status']]}")
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
    total = len(outcomes)
    passed = [item for item in outcomes if item["status"] == OK]
    # 三种错法各数一遍，把「多调了」与「没调够」分开报：混在一个通过率里，
    # 看不出提示词该往哪个方向收。
    over = [item for item in outcomes if item["status"] == OVER]
    under = [item for item in outcomes if item["status"] == UNDER]
    wrong = [item for item in outcomes if item["status"] == WRONG]
    wrong_args = [item for item in outcomes if item["status"] == WRONG_ARGS]
    return Report(
        key="routing",
        title="路由层 L1",
        headline=f"模型 {settings.model_provider}:{settings.model_name}，用例 {total} 条",
        passed=len(passed),
        evaluated=total,
        # 只有「选错且未标记」的算失败；标记过的已知缺口单独计数。
        failed=len([item for item in outcomes if item["status"] != OK and not item["case"].expected_fail]),
        known_gaps=len([item for item in outcomes if item["status"] != OK and item["case"].expected_fail]),
        # 五个比率之和为 1：正确率掉下来时，能直接看出是哪种错法占的。
        # 必须放比率而不是计数 —— 报告按 {:.2f} 打印，计数会显示成「2.00」。
        metrics={
            "正确率": len(passed) / total,
            "过度调用率": len(over) / total,
            "调用不足率": len(under) / total,
            "工具选错率": len(wrong) / total,
            "参数填错率": len(wrong_args) / total,
        },
        # 明细文字交给本层自己拼，报告模块只负责按行打印。
        problems=_problems(outcomes),
        ranks={item["case"].name: (1 if item["status"] == OK else None) for item in outcomes},
    )
