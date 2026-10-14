"""把一份或多份原件经加载、清洗、切分后的分片渲染成 HTML，便于肉眼核对切得对不对。

用法：uv run python -m scripts.inspect_chunks <文件...> [输出.html]
"""

import html
import re
import sys
from pathlib import Path

from app.workers.knowledge import cleaner, loader, splitter

STYLE = """
body { margin: 0; padding: 32px; background: #f6f7f9; color: #1f2328;
       font-family: -apple-system, "PingFang SC", sans-serif; }
h1 { font-size: 20px; margin: 36px 0 6px; }
h1:first-of-type { margin-top: 0; }
.sub { color: #636c76; font-size: 13px; margin-bottom: 16px; }
.sub b { font-family: ui-monospace, monospace; color: #1f2328; }
.chart { background: #fff; border: 1px solid #d8dee4; border-radius: 8px;
         padding: 12px 14px; margin-bottom: 16px; }
.row { display: flex; align-items: center; gap: 10px; margin: 3px 0; font-size: 11.5px; }
.lbl { width: 20px; text-align: right; }
.num { width: 46px; }
.lbl, .num { font-family: ui-monospace, monospace; color: #636c76; }
.track { position: relative; flex: 1; height: 13px; background: #eef0f3; border-radius: 3px; }
.bar { height: 100%; border-radius: 3px; background: #3f6fd8; }
.bar.warn { background: #d8a13a; }
.bar.bad { background: #cf222e; }
.mark { position: absolute; top: -3px; bottom: -3px; width: 1px; background: #98a1ac; }
.legend { color: #636c76; font-size: 11.5px; margin-top: 8px; }
.card { background: #fff; border: 1px solid #d8dee4; border-radius: 8px;
        margin-bottom: 14px; overflow: hidden; }
.card > header { padding: 9px 14px; background: #f0f2f5; font-size: 12.5px;
                 color: #57606a; border-bottom: 1px solid #d8dee4; }
.card > header b { font-family: ui-monospace, monospace; color: #1f2328; }
pre { margin: 0; padding: 12px 14px; font-size: 12.5px; line-height: 1.7;
      font-family: ui-monospace, "SF Mono", monospace; white-space: pre-wrap;
      overflow-wrap: anywhere; }
.seam { border-top: 1px dashed #d8dee4; padding: 9px 14px; font-size: 11.5px;
        color: #636c76; font-family: ui-monospace, monospace; line-height: 1.8; }
.seam .k { display: inline-block; width: 64px; color: #98a1ac; }
.tag { display: inline-block; margin-left: 8px; padding: 1px 6px; border-radius: 4px;
       font-size: 11px; }
.tag.warn { background: #fff4e0; color: #9a6700; }
.tag.bad { background: #ffe9e9; color: #cf222e; }
.tag.good { background: #e6f4ea; color: #1a7f37; }
.lost { background: #fff; border: 1px solid #f0a0a0; border-radius: 8px;
        padding: 12px 14px; margin-bottom: 16px; font-size: 12.5px; color: #cf222e; }
"""

SEPARATOR = re.compile(r"^\s*\|[\s:|-]*\|\s*$")
WORD = re.compile(r"[A-Za-z0-9%]{2,}|[\u4e00-\u9fff]{2,}")


def table_runs(text: str) -> list[tuple[int, bool]]:
    """找出内容里的表格行块，返回（行数, 是否带分隔行）。"""
    lines = text.splitlines()
    runs: list[tuple[int, bool]] = []
    start: int | None = None
    for index, line in enumerate(lines + [""]):
        row = line.lstrip().startswith("|")
        if row and start is None:
            start = index
        elif not row and start is not None:
            block = lines[start:index]
            runs.append((len(block), len(block) > 1 and SEPARATOR.match(block[1]) is not None))
            start = None
    return runs


def page_label(chunk) -> str:
    """分片的页码写法：单页写「第 3 页」，跨页写区间。"""
    if chunk.page_start is None or chunk.page_end is None:
        return ""
    if chunk.page_start == chunk.page_end:
        return f"第 {chunk.page_start} 页"
    return f"第 {chunk.page_start}-{chunk.page_end} 页"


def render_chart(chunks) -> str:
    """按片长画条形图，竖线标出目标上限，一眼看出碎片与超长。"""
    longest = max([len(c.content) for c in chunks] + [splitter.CHUNK_SIZE])
    mark = splitter.CHUNK_SIZE / longest * 100
    rows = []
    for chunk in chunks:
        size = len(chunk.content)
        cls = "bad" if size > splitter.CHUNK_SIZE else "warn" if size < splitter.MIN_CHUNK_SIZE else ""
        rows.append(
            f'<div class="row"><span class="lbl">{chunk.ordinal:02d}</span>'
            f'<div class="track"><div class="bar {cls}" style="width:{size / longest * 100:.1f}%"></div>'
            f'<div class="mark" style="left:{mark:.2f}%"></div></div>'
            f'<span class="num">{size}</span></div>'
        )
    legend = (
        f'<div class="legend">竖线 = 上限 {splitter.CHUNK_SIZE} 字 · 黄色 = 碎片（&lt; '
        f'{splitter.MIN_CHUNK_SIZE} 字）· 红色 = 超长</div>'
    )
    return f'<div class="chart">{"".join(rows)}{legend}</div>'


def render_chunk(index: int, chunk, following) -> str:
    """渲染一个分片：头部标签、正文、与下一片的接缝。"""
    size = len(chunk.content)
    tags = []
    if size > splitter.CHUNK_SIZE:
        tags.append('<span class="tag bad">超长</span>')
    if size < splitter.MIN_CHUNK_SIZE:
        tags.append('<span class="tag warn">碎片</span>')
    runs = table_runs(chunk.content)
    if runs:
        whole = all(has for _, has in runs)
        tags.append(
            f'<span class="tag {"good" if whole else "bad"}">表格 {len(runs)} 段 · '
            f'{"带表头" if whole else "缺表头"}</span>'
        )

    label = f'<b>[{index:02d}] {chunk.kind}</b> · {size} 字 · {page_label(chunk)}'
    if chunk.heading:
        label += f" · {html.escape(chunk.heading)}"

    seam = ""
    if following is not None:
        lines = chunk.content.splitlines()
        tail = html.escape(lines[-1][:64]) if lines else ""
        head = html.escape(following.content.splitlines()[0][:64])
        seam = (
            f'<div class="seam"><span class="k">本片末行</span>{tail}'
            f'<br><span class="k">下片首行</span>{head}</div>'
        )

    body = html.escape(chunk.content)
    return f'<div class="card"><header>{label}{"".join(tags)}</header><pre>{body}</pre>{seam}</div>'


def render_source(source: Path) -> str:
    """跑一遍加载、清洗、切分，渲染一个文件的整节。"""
    suffix = source.suffix.lower()
    blocks = loader.load(suffix, source.read_bytes())
    cleaned = cleaner.clean(blocks)
    chunks = splitter.split(suffix, cleaned)

    if not chunks:
        return f'<h1>{html.escape(source.name)}</h1><div class="sub">没有切出任何分片</div>'

    before = set(WORD.findall(" ".join(block.text for block in cleaned)))
    # 标题行会被切分器从内容里剥出、存进 heading，向量化时再拼回去，所以一并计入。
    texts = [f"{chunk.heading} {chunk.content}" if chunk.heading else chunk.content for chunk in chunks]
    after = set(WORD.findall(" ".join(texts)))
    lost = sorted(before - after)
    tiny = sum(1 for c in chunks if len(c.content) < splitter.MIN_CHUNK_SIZE)

    head = (
        f'<h1>切分预览 · {html.escape(source.name)}</h1>'
        f'<div class="sub">加载 <b>{len(blocks)}</b> 块 → 清洗后 <b>{len(cleaned)}</b> 块'
        f' → 切分 <b>{len(chunks)}</b> 片 · 碎片 <b>{tiny}</b> 个</div>'
    )
    warn = ""
    if lost:
        shown = "、".join(html.escape(word) for word in lost[:24])
        more = f"（共 {len(lost)} 个）" if len(lost) > 24 else ""
        warn = (
            f'<div class="lost">清洗后有、分片里找不到的词：{shown}{more}'
            "<br>落在这里的多是标题词——标题行被切分器剥进 heading，正文里不再出现；"
            "若它还是个「标题下没有正文」的空节，该节不产出分片，这行标题就彻底不在了。</div>"
        )

    cards = [
        render_chunk(index, chunk, chunks[index + 1] if index + 1 < len(chunks) else None)
        for index, chunk in enumerate(chunks)
    ]
    return f"{head}{warn}{render_chart(chunks)}{''.join(cards)}"


def main() -> None:
    """解析参数：以 .html 结尾的最后一项是输出，其余是要预览的原件。"""
    args = sys.argv[1:]
    if not args:
        raise SystemExit("用法：inspect_chunks.py <文件...> [输出.html]")
    out = Path(args[-1]) if args[-1].endswith(".html") else Path("/tmp/knowledge_split_report.html")
    sources = [Path(a) for a in (args[:-1] if args[-1].endswith(".html") else args)]
    body = "".join(render_source(source) for source in sources)
    document = (
        '<!doctype html><html lang="zh"><meta charset="utf-8">'
        f"<style>{STYLE}</style>{body}</html>"
    )
    out.write_text(document, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
