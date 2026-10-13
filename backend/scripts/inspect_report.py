"""把一份原件经加载与清洗的逐行判定渲染成 HTML，便于肉眼核对。

用法：uv run python scripts/inspect_report.py <文件路径> [输出.html]
"""

import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.modules.knowledge import cleaner, loader  # noqa: E402

STYLE = """
body { margin: 0; padding: 32px; background: #f6f7f9; color: #1f2328;
       font-family: -apple-system, "PingFang SC", sans-serif; }
h1 { font-size: 20px; margin: 0 0 6px; }
.sub { color: #636c76; font-size: 13px; margin-bottom: 24px; }
.sub b { font-family: ui-monospace, monospace; color: #1f2328; }
.card { background: #fff; border: 1px solid #d8dee4; border-radius: 8px;
        margin-bottom: 14px; overflow: hidden; }
.card > header { padding: 9px 14px; background: #f0f2f5; font-size: 12.5px;
                 color: #57606a; border-bottom: 1px solid #d8dee4; }
.card > header b { font-family: ui-monospace, monospace; color: #1f2328; }
pre { margin: 0; padding: 12px 14px; font-size: 12.5px; line-height: 1.7;
      font-family: ui-monospace, "SF Mono", monospace; white-space: pre-wrap;
      overflow-wrap: anywhere; }
.line { display: block; }
.drop { color: #cf222e; text-decoration: line-through;
        text-decoration-color: #f0a0a0; }
.tag { display: inline-block; margin-left: 8px; padding: 1px 6px; border-radius: 4px;
       font-size: 11px; background: #ffe9e9; color: #cf222e; text-decoration: none; }
"""


def walk(block, repeated: set[str]) -> list[tuple[str, bool, str]]:
    """重现清洗层对该块每一行的判定，返回（行文本, 是否删除, 原因）。"""
    if block.kind != "paragraph":
        return [(line, False, "结构块原样放过") for line in block.text.splitlines() if line.strip()]
    if block.source != "pdf":
        return [(line, False, "结构化文本只整理空白") for line in block.text.splitlines() if line.strip()]
    out = []
    for raw in block.text.splitlines():
        line = re.sub(r"[ \t]+", " ", cleaner._strip_invisible(raw)).strip()
        if not line:
            continue
        if cleaner._fingerprint(line) in repeated:
            out.append((line, True, "跨页重复"))
        elif cleaner._is_page_number(line, block.page):
            out.append((line, True, "页码"))
        else:
            out.append((line, False, ""))
    return out


def render(source: Path) -> str:
    """读原件，逐块渲染清洗前后对照。"""
    blocks = loader.load(source.suffix.lower(), source.read_bytes())
    cleaned = cleaner.clean(blocks)
    repeated = cleaner._repeated_lines(blocks)

    cards = []
    for block in blocks:
        lines = walk(block, repeated)
        if not lines:
            continue
        removed = sum(1 for _, drop, _ in lines if drop)
        state = f"删 {removed} 行" if removed else "全部保留"
        rows = []
        for text, drop, reason in lines:
            body = html.escape(text)
            if drop:
                rows.append(f'<span class="line drop">{body}<span class="tag">{reason}</span></span>')
            else:
                rows.append(f'<span class="line">{body}</span>')
        label = f"{block.kind}/{block.source}/p{block.page}"
        if block.heading:
            label += f" · {html.escape(block.heading)}"
        cards.append(
            f'<div class="card"><header><b>{label}</b>'
            f' · {len(block.text)} 字 · {state}</header><pre>{"".join(rows)}</pre></div>'
        )

    head = (
        f'<h1>清洗核对 · {html.escape(source.name)}</h1>'
        f'<div class="sub">加载 <b>{len(blocks)}</b> 块 → 清洗后 <b>{len(cleaned)}</b> 块'
        f' · 判定为跨页重复的行 <b>{len(repeated)}</b> 条</div>'
    )
    style = f"<style>{STYLE}</style>"
    return f'<!doctype html><html lang="zh"><meta charset="utf-8">{style}{head}{"".join(cards)}</html>'


def main() -> None:
    source = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/knowledge_clean_report.html")
    out.write_text(render(source), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
