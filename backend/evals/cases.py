"""用例是数据：从 TOML 读入并校验，执行方式留在各自的层里。

用例写成文件而不是写死在代码里，是为了让「加一条问句」不用碰 Python：
新增或修改用例只动 `cases/*.toml`，判定逻辑与报告格式都保持不变。
本模块只负责读进来、校验、转成结构化的对象，不关心怎么跑、按什么判。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

# 用例文件所在的默认目录，与本模块同级；文件名按层命名（如 retrieval.toml）。
CASES_DIR = Path(__file__).with_name("cases")


@dataclass(frozen=True)
class RetrievalCase:
    """一条检索用例。

    期望写成「出处 + 内容里必须出现的原话」，不锁片段顺序与相似度数值：
    换模型或调召回只要还能把这一段取回来，就该通过。expect_doc 与 expect_any
    至少要有一个，只写问题不写期望就无从判定。

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


def load_retrieval_cases(path: Path | None = None) -> list[RetrievalCase]:
    """读用例文件并校验；缺问题或缺期望直接报错，不静默跳过一条用例。

    传 `path` 可以读指定的用例文件，省略则读 `cases/retrieval.toml`。
    校验规则（任一不满足都抛 ValueError，让问题在跑之前就暴露）：
    - 文件里至少要有一条用例；
    - `name` 不能为空、也不能重复 —— 报告与基线都按名字定位，重名会互相覆盖；
    - `query` 不能为空，且 `expect_doc` 与 `expect_any` 至少要有一个。
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
        if not query or not (item.get("expect_doc") or expect_any):
            raise ValueError(f"用例 {name} 缺少问题或期望")
        cases.append(
            RetrievalCase(
                name=name,
                query=query,
                expect_doc=item.get("expect_doc"),
                expect_any=expect_any,
                # 未写明 top_k 时取 5，与线上检索的取值保持一致。
                top_k=int(item.get("top_k", 5)),
                expected_fail=bool(item.get("expected_fail", False)),
                note=str(item.get("note") or ""),
            )
        )
    if not cases:
        raise ValueError(f"{source} 里没有任何用例")
    return cases
