"""按文档类型切分：先按结构切开，再递归切到目标长度，最后并掉碎片。"""

import re
from dataclasses import dataclass

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from app.workers.knowledge.loader import LoadedBlock

CHUNK_SIZE = 800
CHUNK_OVERLAP = 120
# 低于这个长度的分片会被并进前一片，避免几十字的小块单独成为检索单位。
MIN_CHUNK_SIZE = 300

# 中文不以空格分词，分隔符要带上全角标点，否则会在句子中间硬断。
SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "…", "，", " ", ""]

MARKDOWN_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]

# Markdown 表格的分隔行，形如 |---|:--:|
_TABLE_SEPARATOR = re.compile(r"^\s*\|[\s:|-]*\|\s*$")

_RECURSIVE = RecursiveCharacterTextSplitter(
    separators=SEPARATORS, chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
)
_BY_HEADING = MarkdownHeaderTextSplitter(headers_to_split_on=MARKDOWN_HEADERS)

# 切分中的一段：正文、标题路径、页面区间、块类型。
Piece = tuple[str, str | None, int | None, int | None, str]


@dataclass(frozen=True)
class Chunk:
    """待向量化的分片。标题路径、页面区间与块类型是来源信息，检索结果要展示给用户。"""

    ordinal: int
    content: str
    heading: str | None
    page_start: int | None
    page_end: int | None
    kind: str


def split(suffix: str, blocks: list[LoadedBlock]) -> list[Chunk]:
    """切出待入库的分片，序号按最终顺序连续编排。"""
    pieces = _merge(_sections(suffix, blocks))
    return [
        Chunk(
            ordinal=index,
            content=text,
            heading=heading,
            page_start=start,
            page_end=end,
            kind=kind,
        )
        for index, (text, heading, start, end, kind) in enumerate(pieces)
    ]


def _sections(suffix: str, blocks: list[LoadedBlock]) -> list[Piece]:
    """产出「正文 + 标题路径 + 页面区间 + 块类型」，切分与来源信息在这一步合流。"""
    pieces: list[Piece] = []
    if suffix in (".md", ".markdown"):
        for block in blocks:
            for section in _BY_HEADING.split_text(block.text):
                # 标题层级按 h1 → h4 的顺序写进 metadata，拼起来就是所在小节的位置。
                heading = " > ".join(section.metadata.values()) or None
                _extend(pieces, section.page_content, heading, None, block.kind)
    else:
        for block in blocks:
            _extend(pieces, block.text, block.heading, block.page, block.kind)
    return pieces


def _extend(pieces: list[Piece], text: str, heading: str | None, page: int | None, kind: str) -> None:
    """把一块内容切成若干分片后追加进结果。

    表格按行切并把表头复制到每一片；正文走通用递归切分。标题切分只看层级不看长度，
    一节写满几千字也只会切出一块，所以按标题切完还要再过一遍长度切分。
    """
    pieces_: list[str] = _split_table(text) if _is_table(text) else _RECURSIVE.split_text(text)
    for piece in pieces_:
        piece = piece.strip()
        if piece:
            pieces.append((piece, heading, page, page, kind))


def _is_table(text: str) -> bool:
    """判断内容是不是一整张 Markdown 表格：每行都以竖线开头，且第二行是分隔行。"""
    lines = [line for line in text.splitlines() if line.strip()]
    return (
        len(lines) >= 2
        and _TABLE_SEPARATOR.match(lines[1]) is not None
        and all(line.lstrip().startswith("|") for line in lines)
    )


def _split_table(text: str) -> list[str]:
    """按行切表格，每一片都复制表头与分隔行。

    表格的一行是一个语义单元，不能按字数从中间断开——断开后没有表头的那些行说不清
    属于哪一列。所以整表够短就原样返回，超长则按行分组，每组都带上完整表头。
    """
    lines = text.splitlines()
    head, rows = lines[:2], lines[2:]
    if not rows:
        return [text]
    pieces: list[str] = []
    current: list[str] = []
    for row in rows:
        current.append(row)
        # 至少留下一行数据，单行加表头就超限时不再拆。
        if len(current) > 1 and len("\n".join(head + current)) > CHUNK_SIZE:
            current.pop()
            pieces.append("\n".join(head + current))
            current = [row]
    if current:
        pieces.append("\n".join(head + current))
    return pieces


def _merge(pieces: list[Piece]) -> list[Piece]:
    """把短的相邻分片并进前一片，页码随之扩成区间。

    只在「同一标题子树下、块类型相同、并完不超 CHUNK_SIZE」时才并。合并后沿用前片的
    标题，后片自己那一级小节标题作为正文补回去（否则合完那行标题就没了）。表格不参与
    合并：两张表粘在一起会连成一张，行义就变了。
    """
    merged: list[Piece] = []
    for text, heading, start, end, kind in pieces:
        if merged:
            prev = merged[-1]
            common = _common_heading(prev[1], heading)
            joinable = (
                common is not None
                and kind != "table"
                and len(text) < MIN_CHUNK_SIZE
                and prev[4] == kind
                and len(prev[0]) + len(text) + 1 <= CHUNK_SIZE
            )
            if joinable:
                # 后片自己带的小节标题（超出前片标题的那几级）要留下，否则合完就没了；
                # 合并后仍用前片的标题，片子以它的内容开头，标题不会失真。
                extra = _heading_extra(common, heading)
                body = f"{extra}\n{text}" if extra else text
                merged[-1] = (f"{prev[0]}\n{body}", prev[1], prev[2], end, prev[4])
                continue
        merged.append((text, heading, start, end, kind))
    return merged


def _heading_extra(common: str | None, heading: str | None) -> str:
    """标题里超出 common 的那几级，合并后会作为正文的一行保留下来。"""
    if not heading:
        return ""
    depth = len(common.split(" > ")) if common else 0
    return " > ".join(heading.split(" > ")[depth:])


def _common_heading(one: str | None, other: str | None) -> str | None:
    """两段标题路径的共同前缀，按层级对齐；任一段缺标题、或没有共同层级则不合并。"""
    if not one or not other:
        return None
    shared = [
        left for left, right in zip(one.split(" > "), other.split(" > "), strict=False) if left == right
    ]
    return " > ".join(shared) if shared else None
