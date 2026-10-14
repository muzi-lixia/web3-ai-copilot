"""把上传的原件解析成带结构信息的文本块。只做提取，不清洗也不切分。"""

from collections import Counter
from dataclasses import dataclass
from io import BytesIO

import pdfplumber
from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

# Word 把标题层级写在样式名里，中英文版分别是 Heading N 与 标题 N。
HEADING_PREFIXES = ("Heading", "标题")

# 表格的单元格天生是短标签；整段正文被框成表格时，每格会有几百字。
MAX_CELL_CHARS = 100

# PDF 没有标题语法，只能按版式反推：字体名里带这些词的算加粗。
BOLD_MARKERS = ("Bold", "Black", "Heavy")


@dataclass(frozen=True)
class LoadedBlock:
    """一段连续的原文，带上它所属的标题路径、页码、块类型与来源格式。

    标题路径与页码给不出时为空；kind 取 paragraph / table。
    """

    text: str
    heading: str | None = None
    page: int | None = None
    kind: str = "paragraph"
    source: str = ""


def load(suffix: str, content: bytes) -> list[LoadedBlock]:
    """按扩展名分派解析器。类型在入口已校验，这里按纯文本兜底。

    Args:
        suffix: 文件扩展名
        content: 文件原始字节内容

    Returns:
        解析后的文本块列表，每个块包含文本、标题路径、页码等结构信息。
    """
    if suffix == ".pdf":
        return _load_pdf(content)
    if suffix == ".docx":
        return _load_docx(content)
    source = "markdown" if suffix in (".md", ".markdown") else "text"
    return [LoadedBlock(_decode(content), source=source)]


def _decode(content: bytes) -> str:
    """先按 UTF-8 解，失败退 GBK（中文文本文件常见），都不行则替换非法字节。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _load_pdf(content: bytes) -> list[LoadedBlock]:
    """逐页切块：表格各自一块，正文按标题再分段，页码随块带出。

    正文基准字号与标题栈都跨页保留：一页开头的正文才能带上上一页末尾标题的路径。
    """
    blocks: list[LoadedBlock] = []
    with pdfplumber.open(BytesIO(content)) as pdf:
        body_size = _body_size(pdf.pages)
        titles: list[tuple[float, str]] = []
        for page in pdf.pages:
            blocks.extend(_page_blocks(page, body_size, titles))
    return blocks


def _body_size(pages) -> float:
    """全篇字符数最多的字号即正文基准，标题总比它大。"""
    counter: Counter[float] = Counter()
    for page in pages:
        counter.update(round(char["size"], 1) for char in page.chars)
    return counter.most_common(1)[0][0] if counter else 0.0


def _page_blocks(page, body_size: float, titles: list[tuple[float, str]]) -> list[LoadedBlock]:
    """一页拆成若干块：表格各自一块，表格之间的文本按标题再分段，顺序按页内纵向位置。"""
    strips = []
    cursor = 0.0
    for rows, top, bottom in sorted(_tables(page), key=lambda item: item[1]):
        # 处理表格上方的文本（cursor 到 top 之间）
        if top > cursor:
            strips.append((cursor, top, None))
        # 记录表格区域
        strips.append((top, bottom, rows))
        # 游标移到表格底部；表格框可能互相嵌套，只进不退，否则同一区域会被取两次
        cursor = max(cursor, bottom)
    # 处理最后一个表格下方的文本
    if cursor < page.height:
        strips.append((cursor, page.height, None))

    blocks: list[LoadedBlock] = []
    for top, bottom, rows in strips:
        if rows is None:
            blocks.extend(_text_blocks(page, top, bottom, body_size, titles))
            continue
        text = _markdown_table(rows)
        if text:
            blocks.append(
                LoadedBlock(text, _stack_path(titles), page.page_number, kind="table", source="pdf")
            )
    return blocks


def _text_blocks(
    page, top: float, bottom: float, body_size: float, titles: list[tuple[float, str]]
) -> list[LoadedBlock]:
    """把一段文本条带按标题行切开：标题进标题栈，其余正文各自成段并带上当前路径。"""
    blocks: list[LoadedBlock] = []
    pending: list[str] = []
    for line in page.crop((0, top, page.width, bottom)).extract_text_lines():
        text = line["text"].strip()
        if not text:
            continue
        size = _line_title(line, body_size)
        if size is None:
            pending.append(text)
            continue
        if pending:
            blocks.append(_paragraph(pending, page.page_number, titles))
            pending = []
        _push_title(titles, size, text)
    if pending:
        blocks.append(_paragraph(pending, page.page_number, titles))
    return blocks


def _paragraph(lines: list[str], number: int, titles: list[tuple[float, str]]) -> LoadedBlock:
    """把攒着的正文行落成一个段落块，带上当前标题路径。"""
    return LoadedBlock("\n".join(lines), _stack_path(titles), number, source="pdf")


def _line_title(line, body_size: float) -> float | None:
    """拿到标题行的字号；不是标题则返回 None。

    PDF 没有标题语法，只能按版式反推：一行里占多数的字号比正文基准大，且字体名带
    加粗字重，才算标题。字号原样返回，标题栈靠它排层级。
    """
    sizes = Counter(round(char["size"], 1) for char in line["chars"])
    if not sizes:
        return None
    size = sizes.most_common(1)[0][0]
    if size <= body_size:
        return None
    fonts = {char["fontname"].split("+")[-1] for char in line["chars"]}
    if not any(marker in font for font in fonts for marker in BOLD_MARKERS):
        return None
    return size


def _push_title(titles: list[tuple[float, str]], size: float, text: str) -> None:
    """标题入栈：字号更大的标题在外层，同级或更浅的标题把对应层级顶掉。"""
    while titles and titles[-1][0] <= size:
        titles.pop()
    titles.append((size, text))


def _stack_path(titles: list[tuple[float, str]]) -> str | None:
    """标题栈拼成一条路径，便于分片带上「出自哪一节」。"""
    return " > ".join(text for _, text in titles) if titles else None


def _tables(page) -> list[tuple[list[list[str]], float, float]]:
    """挑出页面里的真表格，给出「行数据 + 纵向区间」。

    两步：整张表的单元格平均字数超限，说明整页正文被框成了一张表，整张丢掉；通过
    之后再看每一行，单格里塞着长文本的行不是表格行——真表格把紧随其后的正文吞成
    最后一行时就是这样——丢掉这些行，并把表格区间夹到剩下的行上，让出去的部分由
    文本条带接回。
    """
    found = []
    for table in page.find_tables():
        rows = table.extract()
        if not _is_table(rows):
            continue
        kept = [(row, cells) for row, cells in zip(table.rows, rows, strict=True) if _is_table_row(cells)]
        if not kept:
            continue
        top = max(min(row.bbox[1] for row, _ in kept), 0.0)
        bottom = min(max(row.bbox[3] for row, _ in kept), page.height)
        if bottom > top:
            found.append(([cells for _, cells in kept], top, bottom))
    return found


def _is_table(rows: list[list[str | None]]) -> bool:
    """整张表的单元格平均字数超限即不是表格：整页正文被框成表格时每格有几百字。"""
    cells = [cell.strip() for row in rows for cell in row if cell and cell.strip()]
    if not cells:
        return False
    return sum(len(cell) for cell in cells) / len(cells) <= MAX_CELL_CHARS


def _is_table_row(cells: list[str | None]) -> bool:
    """一行的非空单元格都是短标签才算表格行。"""
    filled = [cell.strip() for cell in cells if cell and cell.strip()]
    return bool(filled) and max(len(cell) for cell in filled) <= MAX_CELL_CHARS


def _markdown_table(rows: list[list[str | None]]) -> str:
    """表格转成 Markdown：首个非空行当表头，第二行是分隔行。"""
    cells = [[_cell(cell) for cell in row] for row in rows if any(cell and cell.strip() for cell in row)]
    if not cells:
        return ""
    width = max(len(row) for row in cells)
    lines = [cells[0], ["---"] * width, *cells[1:]]
    return "\n".join("| " + " | ".join(line) + " |" for line in lines)


def _cell(text: str | None) -> str:
    """单元格内容压成一行并转义竖线，否则会破坏 Markdown 的行列结构。"""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def _load_docx(content: bytes) -> list[LoadedBlock]:
    """按正文顺序遍历段落与表格，用标题样式维护标题路径。

    标题靠样式识别：文档没用 Heading 样式（正文全被排成 Normal）时取不到层级，
    此时所有块的 heading 为空，退回纯段落切分。
    """
    # Document() 需要文件路径或文件对象，BytesIO 将内存中的字节包装成文件对象，无需写磁盘
    document = Document(BytesIO(content))
    blocks: list[LoadedBlock] = []
    titles: list[str] = []
    # document.element.body文档的正文部分（XML 结构）.iterchildren()遍历所有子元素（段落、表格等）
    for element in document.element.body.iterchildren():
        # 处理段落
        if element.tag == qn("w:p"):
            # 创建段落对象
            paragraph = Paragraph(element, document)
            # 提取文本去除首尾空白
            text = paragraph.text.strip()
            # 跳过空段
            if not text:
                continue
            style = paragraph.style
            # 判断标题级别
            level = _heading_level(style.name) if style is not None else None
            # 如果不是标题
            if level is None:
                # 处理普通段落
                blocks.append(LoadedBlock(text, _title_path(titles), source="docx"))
                continue
            # 同级或更高级的标题把栈里更深的层级顶掉，保证路径是当前所在的层级链。
            del titles[level - 1 :]  # 删除当前层级及更深的层
            titles.append(text)  # 添加新标题
        # 处理表格
        elif element.tag == qn("w:tbl"):
            text = _table_text(Table(element, document))
            if text:
                blocks.append(LoadedBlock(text, _title_path(titles), kind="table", source="docx"))
    return blocks


def _heading_level(style: str) -> int | None:
    """从样式名取出标题级别，不是标题样式则返回 None。"""
    if not style:
        return None
    prefix, _, number = style.rpartition(" ")
    if prefix in HEADING_PREFIXES and number.isdigit():
        return int(number)
    return None


def _title_path(titles: list[str]) -> str | None:
    """标题层级拼成一条路径，便于分片带上「出自哪一节」。"""
    return " > ".join(titles) if titles else None


def _table_text(table: Table) -> str:
    """表格转成 Markdown；整表作为一个块，不做行组拆分。"""
    return _markdown_table([[cell.text for cell in row.cells] for row in table.rows])
