"""列出检索用例的候选片段，供人工标注相关集合。

每条问句取前若干名候选（比线上检索的 5 名多），勾出「能用来回答它」的片段；
有了这份标注才算得出 precision@K —— 只标一个目标片段时，未标到的候选
既可能是漏召、也可能本来就不相关。

只读语料与检索，不调生成模型、不写库。

用法（在 backend 目录下）：
    .venv/bin/python -m scripts.list_retrieval_candidates
    .venv/bin/python -m scripts.list_retrieval_candidates --limit 10 --out /tmp/candidates.md
"""

import argparse
import asyncio
from pathlib import Path

from app.core.config import get_settings
from app.infrastructure.database.session import Database
from app.modules.knowledge.service import KnowledgeService
from evals.cases import RetrievalCase, load_retrieval_cases

# 候选池默认取前 10 名：标注要能看到「差一点没进线上前 5」的片段。
DEFAULT_LIMIT = 10

# 输出路径默认放评估目录，和用例、基线在一起。
DEFAULT_OUT = Path("evals/retrieval-candidates.md")

# 每条候选只印正文开头这么多字，够判断讲的是不是同一件事。
BRIEF_CHARS = 120


def _brief(content: str) -> str:
    """把片段正文压成单行摘要：空白折成单空格，超长截断。"""
    flat = " ".join(content.split())
    return flat if len(flat) <= BRIEF_CHARS else flat[:BRIEF_CHARS] + "…"


def _heading(hit: dict) -> str:
    """候选所属标题；空标题写明「（无标题）」而不是留白。"""
    title = " ".join((hit["heading"] or "").split())
    return title or "（无标题）"


def _expectation(case: RetrievalCase) -> str:
    """把用例现有的判据写成一行，勾选时用来对照。"""
    doc = case.expect_doc or "任一文档"
    wanted = "、".join(f'"{text}"' for text in case.expect_any) or "（只校验出处）"
    return f"出处 `{doc}`，正文含 {wanted}"


def _render(collected: list[tuple[RetrievalCase, list[dict]]], limit: int) -> str:
    """渲染成可勾选的 Markdown：一条用例一段，候选一行一个。"""
    lines = [
        "# 检索层相关集合标注",
        "",
        f"每条问句列出前 {limit} 名候选（线上检索只用前 5 名）。",
        "把**能用来回答这条问句**的候选改成 `- [x]`，判据是内容本身，与排第几名无关。",
        "一条问句可以勾多个，也可以一个都不勾。勾完把本文件路径交回。",
        "",
    ]
    for index, (case, hits) in enumerate(collected, start=1):
        lines += [
            f"## {index}. `{case.name}`",
            "",
            f"问句：**{case.query}**",
            "",
            f"现有判据：{_expectation(case)}",
            "",
        ]
        if not hits:
            lines.append("- （没有取回任何候选）")
        for rank, hit in enumerate(hits, start=1):
            mark = f"{hit['filename']}#{hit['ordinal']}"
            lines.append(f"- [ ] `{mark}` · 第 {rank} 名 · {hit['score']:.4f} · {_heading(hit)}")
            lines.append(f"      {_brief(hit['content'])}")
        lines.append("")
    return "\n".join(lines)


async def _run(limit: int, out: Path) -> int:
    """逐条检索取候选，渲染后写出文件；环境不通时直接报错退出。"""
    settings = get_settings()
    database = Database(settings.foundation_database_url or settings.database_url)
    try:
        service = KnowledgeService(database, settings)
        collected: list[tuple[RetrievalCase, list[dict]]] = []
        for case in load_retrieval_cases():
            hits = (await service.search(case.query, limit))["hits"]
            collected.append((case, hits))
    except Exception as error:
        print(f"取候选失败：{type(error).__name__}: {error}")
        return 2
    finally:
        await database.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render(collected, limit), encoding="utf-8")
    print(f"已写出 {out}：{len(collected)} 条用例，每条最多 {limit} 个候选")
    return 0


def main() -> int:
    """命令行入口：层内只收候选条数与输出路径。"""
    parser = argparse.ArgumentParser(description="列检索用例的候选片段供人工标注")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="每条问句取前多少名候选")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出文件路径")
    arguments = parser.parse_args()
    return asyncio.run(_run(arguments.limit, arguments.out))


if __name__ == "__main__":
    raise SystemExit(main())
