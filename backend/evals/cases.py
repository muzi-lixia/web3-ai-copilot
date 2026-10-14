"""用例是数据：从 TOML 读入并校验，执行方式留在各自的层里。

用例写成文件而不是写死在代码里，是为了让「加一条问句」不用碰 Python：
新增或修改用例只动 `cases/*.toml`，判定逻辑与报告格式都保持不变。
本模块只负责读进来、校验、转成结构化的对象，不关心怎么跑、按什么判。
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 用例文件所在的默认目录，与本模块同级；文件名按层命名（如 retrieval.toml）。
CASES_DIR = Path(__file__).with_name("cases")


@dataclass(frozen=True)
class RetrievalCase:
    """一条检索用例。

    期望写成「出处 + 内容里必须出现的原话」，不锁片段顺序与相似度数值：
    换模型或调召回只要还能把这一段取回来，就该通过。expect_doc 与 expect_any
    至少要有一个；无答案用例则使用 expect_empty，不能与命中期望混用。

    字段：
    - `name`        用例名，报告与基线比对都按它定位，必须唯一。
    - `query`       送进检索的原始问句，就是用户会打的那句话。
    - `expect_doc`  期望命中的文档，匹配片段的 `filename` 或 `title`；可为空。
    - `expect_any`  期望在片段正文里出现的原话，命中任意一条即可；可为空。
    - `top_k`       取前多少名参与判定，与线上检索取值一致才有意义。
    - `expected_fail`
      标的是**已知缺口**：期望合理、当前确实取不到，且原因写在 note 里。
      它照常计入 MRR，但不把整轮判成失败；等到它能命中了，
      报告会提醒移除这个标记。
    - `note`        备注，只在「已知缺口」段落里展示，用来说明为什么现在取不到。

    frozen=True：用例从文件读入后只读，避免执行过程中被判定逻辑改掉。
    """

    name: str
    query: str
    expect_doc: str | None
    expect_any: tuple[str, ...]
    top_k: int
    expected_fail: bool = False
    note: str = ""
    expect_empty: bool = False
    requires_docs: tuple[str, ...] = ()


def load_retrieval_cases(path: Path | None = None) -> list[RetrievalCase]:
    """读用例文件并校验；缺问题或缺期望直接报错，不静默跳过一条用例。

    传 `path` 可以读指定的用例文件，省略则读 `cases/retrieval.toml`。
    校验规则（任一不满足都抛 ValueError，让问题在跑之前就暴露）：
    - 文件里至少要有一条用例；
    - `name` 不能为空、也不能重复 —— 报告与基线都按名字定位，重名会互相覆盖；
    - `query` 不能为空，且声明命中期望或 expect_empty，二者不能混用。
    """
    source = path or CASES_DIR / "retrieval.toml"
    # tomllib 是标准库，用例文件不需要任何额外依赖；读文件固定用 UTF-8。
    raw = tomllib.loads(source.read_text(encoding="utf-8"))
    cases: list[RetrievalCase] = []
    seen: set[str] = set()
    # TOML 里每条用例写成一个 [[case]] 表，这里按文件里的出现顺序逐条读入。
    for item in raw.get("case", []):
        name = str(item.get("name") or "")
        if not name or name in seen:
            raise ValueError(f"用例名缺失或重复：{name!r}")
        seen.add(name)
        query = str(item.get("query") or "")
        # expect_any 允许写多条原话；缺省成空元组，表示只校验出处。
        expect_any = tuple(str(text) for text in item.get("expect_any") or ())
        # 只写问题、不写任何期望的用例无法判定，直接报错而不是当成必然失败。
        expect_empty = item.get("expect_empty", False)
        if not isinstance(expect_empty, bool):
            raise ValueError(f"用例 {name} 的 expect_empty 必须是布尔值")
        if expect_empty and (item.get("expect_doc") or expect_any):
            raise ValueError(f"用例 {name} 的无答案期望不能同时声明命中片段")
        if not query or not (item.get("expect_doc") or expect_any or expect_empty):
            raise ValueError(f"用例 {name} 缺少问题或期望")
        cases.append(
            RetrievalCase(
                name=name,
                query=query,
                expect_doc=item.get("expect_doc"),
                expect_any=expect_any,
                # 未写明 top_k 时取 5，与线上检索的取值保持一致。
                top_k=int(item.get("top_k", 5)),
                expect_empty=expect_empty,
                requires_docs=tuple(item.get("requires_docs") or ()),
                expected_fail=bool(item.get("expected_fail", False)),
                note=str(item.get("note") or ""),
            )
        )
    if any(case.top_k < 1 or case.top_k > 20 for case in cases):
        raise ValueError("检索用例 top_k 必须在 1 到 20 之间")
    if not cases:
        raise ValueError(f"{source} 里没有任何用例")
    return cases


@dataclass(frozen=True)
class RewriteCase:
    """一条改写用例。

    断言只落在「改写后的问题」上：要么必须带上某些字（`expect_any`），要么必须与
    用户原话一致（`expect_same`）。不锁措辞 —— 换模型只要该补的指代补上了、
    不该改的没乱改，就该通过。

    字段：
    - `name`        用例名，报告与基线比对都按它定位，必须唯一。
    - `history`     本轮之前的对话，(角色, 内容) 按顺序排列；角色只取 user / assistant。
    - `query`       用户这一轮的原话，就是要被改写的那句。
    - `expect_any`  改写结果里必须出现其中任意一条；可为空。
    - `expect_same` 期望改写结果与 `query` 一致（忽略首尾空白与末尾标点）。
    - `expected_fail`
      标的是**已知缺口**：期望合理、当前模型确实做不到，且原因写在 note 里。
      它不把整轮判成失败；等它真能通过了，报告会提醒移除这个标记。
    - `note`        备注，只在「已知缺口」段落里展示。

    frozen=True：用例从文件读入后只读，避免执行过程中被判定逻辑改掉。
    """

    name: str
    history: tuple[tuple[str, str], ...]
    query: str
    expect_any: tuple[str, ...]
    expect_same: bool = False
    expected_fail: bool = False
    note: str = ""


def _history_pair(entry: dict, name: str) -> tuple[str, str]:
    """把 TOML 里的一条历史写成 (角色, 内容)。

    角色只收 user / assistant：拼消息时靠角色决定是人还是助手说的，
    写别的角色拼不出来，直接报错比默默跳过一条好。
    """
    role = str(entry.get("role") or "")
    if role not in ("user", "assistant"):
        raise ValueError(f"用例 {name} 的历史角色只能是 user / assistant：{role!r}")
    return role, str(entry.get("content") or "")


def load_rewrite_cases(path: Path | None = None) -> list[RewriteCase]:
    """读改写用例并校验；缺问题或缺期望直接报错，不静默跳过一条用例。

    传 `path` 可以读指定的用例文件，省略则读 `cases/rewrite.toml`。
    校验规则（任一不满足都抛 ValueError）：
    - 文件里至少要有一条用例；
    - `name` 不能为空、也不能重复 —— 报告与基线都按名字定位，重名会互相覆盖；
    - `query` 不能为空，且 `expect_any` 与 `expect_same` 至少要有一个；
    - `history` 里的角色只能是 user / assistant。
    """
    source = path or CASES_DIR / "rewrite.toml"
    raw = tomllib.loads(source.read_text(encoding="utf-8"))
    cases: list[RewriteCase] = []
    seen: set[str] = set()
    # TOML 里每条用例写成一个 [[case]] 表，这里按文件里的出现顺序逐条读入。
    for item in raw.get("case", []):
        name = str(item.get("name") or "")
        if not name or name in seen:
            raise ValueError(f"用例名缺失或重复：{name!r}")
        seen.add(name)
        query = str(item.get("query") or "")
        # expect_any 允许写多条；缺省成空元组，表示只看有没有乱改。
        expect_any = tuple(str(text) for text in item.get("expect_any") or ())
        expect_same = bool(item.get("expect_same", False))
        # 只写问题、不写任何期望的用例无法判定，直接报错而不是当成必然失败。
        if not query or not (expect_any or expect_same):
            raise ValueError(f"用例 {name} 缺少问题或期望")
        cases.append(
            RewriteCase(
                name=name,
                history=tuple(_history_pair(entry, name) for entry in item.get("history") or ()),
                query=query,
                expect_any=expect_any,
                expect_same=expect_same,
                expected_fail=bool(item.get("expected_fail", False)),
                note=str(item.get("note") or ""),
            )
        )
    if not cases:
        raise ValueError(f"{source} 里没有任何用例")
    return cases


@dataclass(frozen=True)
class RoutingCase:
    """一条工具路由用例。

    断言落在「这一轮该调哪个工具、参数填得对不对」上，不看措辞。参数的格式（类型、
    取值范围）由工具自己的 schema 兜住，兜不住的是「该不该填」—— 点了币种却没填、
    没点却硬填，这类只能在这一层量，所以有了 `expect_args`。

    字段：
    - `name`        用例名，报告与基线比对都按它定位，必须唯一。
    - `query`       用户这一轮的原话。
    - `history`     本轮之前的对话，(角色, 内容) 按顺序排列；角色只取 user / assistant。
    - `expect_tools`
      首轮必须调用其中至少一个。写「至少一个」而不是「全部」：一个问题常有单个
      工具就能答完，要求全调会把「多调了」也判成错，那不是这一层要量的东西。
    - `forbid_tools`
      首轮不允许出现其中任何一个。与上一条的方向相反，用来卡住那些「看起来相近、
      其实答不了」的工具。
    - `expect_no_tools`
      首轮不该调用任何工具；与 `expect_tools` 互斥，写在一起无从判定。
    - `max_calls`
      首轮调用条数的上限，缺省表示不限制。用来量**过度调用**：一个问题一次就够时
      写 1，多调一个「既不在期望、也不在禁区」的工具同样算错 —— 只写禁区卡不住它。
    - `expect_args`
      期望该工具收到的参数，按子集比对（默认值等价于省略，写 `symbols = []` 时
      模型整条不传也算对）。只在需要区分「填什么」的用例上写 —— 它回答不了
      「该调哪个工具」，所以必须同时写 `expect_tools`。
    - `expected_fail`
      标的是**已知缺口**：期望合理、当前模型确实选不对，且原因写在 note 里。
      它不把整轮判成失败；等它真能选对了，报告会提醒移除这个标记。
    - `note`       备注，只在「已知缺口」段落里展示。

    frozen=True：用例从文件读入后只读，避免执行过程中被判定逻辑改掉。
    """

    name: str
    query: str
    history: tuple[tuple[str, str], ...]
    expect_tools: tuple[str, ...]
    forbid_tools: tuple[str, ...]
    expect_no_tools: bool = False
    max_calls: int | None = None
    expect_args: dict[str, Any] = field(default_factory=dict)
    expected_fail: bool = False
    note: str = ""


def load_routing_cases(path: Path | None = None) -> list[RoutingCase]:
    """读路由用例并校验；缺问题或缺期望直接报错，不静默跳过一条用例。

    传 `path` 可以读指定的用例文件，省略则读 `cases/routing.toml`。
    校验规则（任一不满足都抛 ValueError）：
    - 文件里至少要有一条用例；
    - `name` 不能为空、也不能重复 —— 报告与基线都按名字定位，重名会互相覆盖；
    - `query` 不能为空，且三条期望至少要写一条；
    - `expect_no_tools` 不能与 `expect_tools` 同时写：一个要求不调、一个要求必调；
    - `max_calls` 写了就要 ≥ 1（要「一个都不调」就写 `expect_no_tools`）；
    - `expect_args` 写了就要同时写 `expect_tools` —— 否则不知道该比对哪一次调用；
    - `history` 里的角色只能是 user / assistant。

    工具名对不对不在这里查：那需要工具注册表，本模块只读用例文件。
    """
    source = path or CASES_DIR / "routing.toml"
    raw = tomllib.loads(source.read_text(encoding="utf-8"))
    cases: list[RoutingCase] = []
    seen: set[str] = set()
    # TOML 里每条用例写成一个 [[case]] 表，这里按文件里的出现顺序逐条读入。
    for item in raw.get("case", []):
        name = str(item.get("name") or "")
        if not name or name in seen:
            raise ValueError(f"用例名缺失或重复：{name!r}")
        seen.add(name)
        query = str(item.get("query") or "")
        # 两条工具名清单都缺省成空元组，表示不设要求。
        expect_tools = tuple(str(text) for text in item.get("expect_tools") or ())
        forbid_tools = tuple(str(text) for text in item.get("forbid_tools") or ())
        expect_no_tools = bool(item.get("expect_no_tools", False))
        # max_calls 缺省 None = 不限条数；写了就要求首轮调用条数不超过它。
        max_calls = item.get("max_calls")
        if max_calls is not None and int(max_calls) < 1:
            raise ValueError(f"用例 {name} 的 max_calls 必须至少为 1：{max_calls!r}")
        # expect_args 缺省为空表：只比对写出来的键，其余参数不限制。
        expect_args = dict(item.get("expect_args") or {})
        if expect_args and not expect_tools:
            raise ValueError(f"用例 {name} 写了 expect_args 却没写 expect_tools，无从比对")
        # 只写问题、不写任何期望的用例无法判定，直接报错而不是当成必然失败。
        if not query or not (expect_tools or forbid_tools or expect_no_tools or max_calls):
            raise ValueError(f"用例 {name} 缺少问题或期望")
        if expect_no_tools and expect_tools:
            raise ValueError(f"用例 {name} 同时要求「必调」与「不调」，无法判定")
        cases.append(
            RoutingCase(
                name=name,
                query=query,
                history=tuple(_history_pair(entry, name) for entry in item.get("history") or ()),
                expect_tools=expect_tools,
                forbid_tools=forbid_tools,
                expect_no_tools=expect_no_tools,
                max_calls=int(max_calls) if max_calls is not None else None,
                expect_args=expect_args,
                expected_fail=bool(item.get("expected_fail", False)),
                note=str(item.get("note") or ""),
            )
        )
    if not cases:
        raise ValueError(f"{source} 里没有任何用例")
    return cases
