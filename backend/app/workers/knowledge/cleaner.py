"""清洗解析结果。力度按来源分：PDF 要压制版面噪点，结构化文本只整理空白。"""

import re
from collections import Counter
from dataclasses import replace

from app.workers.knowledge.loader import LoadedBlock

# 页眉页脚总待在页面边上，所以只在每页的头尾各取两行当候选；正文里跨页重复的短句
# 不在这两个位置上，天然不会被误伤。比对前先抹掉数字，页码不同的同一行页脚也能
# 归成一个指纹；跨页重复且出现在过半页面上才算数。
EDGE_LINES = 2
REPEAT_RATIO = 0.6
MIN_PAGES = 2

_PAGE_NUMBER = re.compile(r"[-—\s]*(?:第\s*)?(\d{1,4})\s*页?[-—\s]*")
_BLANK_LINES = re.compile(r"\n{3,}")
_DIGITS = re.compile(r"\d+")
_SPACES = re.compile(r"\s+")
_ZERO_WIDTH = re.compile(r"[\u200b\ufeff]")
# 行尾连字符是排版折行留下的：小写续行要接成一个词，大写续行是连字，保留连字符。
_HYPHEN_JOIN = re.compile(r"([A-Za-z]{2,})-\n([a-z]{2,})")
_HYPHEN_KEEP = re.compile(r"([A-Za-z])-\n([A-Za-z])")


def clean(blocks: list[LoadedBlock]) -> list[LoadedBlock]:
    """逐块清洗并丢掉清洗后为空的块。表格这类结构原样保留，其余按来源选力度。"""
    if not blocks:
        return []
    repeated = _repeated_lines(blocks)
    texts = []
    for block in blocks:
        if block.kind != "paragraph":
            texts.append(block.text)
        elif block.source == "pdf":
            texts.append(_from_pdf(block.text, repeated, block.page))
        else:
            texts.append(_normalize(block.text))
    return [replace(block, text=text) for block, text in zip(blocks, texts, strict=True) if text]


def _repeated_lines(blocks: list[LoadedBlock]) -> set[str]:
    """找出每页头尾反复出现的行，返回它们的指纹。

    按页把段落行拼起来，每页只取头尾各两行作候选；行数太少的页面上这两段会覆盖
    整页，判不出头尾，跳过不参与。够长的页面不足两页时也不判，免得单页文档的头
    两行被整段删掉。
    """
    gathered: dict[int, list[str]] = {}
    for block in blocks:
        if block.kind != "paragraph" or block.page is None:
            continue
        gathered.setdefault(block.page, []).extend(
            line.strip() for line in block.text.splitlines() if line.strip()
        )
    pages = [lines for lines in gathered.values() if len(lines) > EDGE_LINES * 2]
    if len(pages) < MIN_PAGES:
        return set()

    counter: Counter[str] = Counter()
    for lines in pages:
        edges = lines[:EDGE_LINES] + lines[-EDGE_LINES:]
        # 同页内一次去重，页面只有三两行时同一行不会被计两次。
        for fingerprint in {_fingerprint(line) for line in edges}:
            if fingerprint:
                counter[fingerprint] += 1
    total = len(pages)
    return {line for line, count in counter.items() if count / total >= REPEAT_RATIO}


def _fingerprint(line: str) -> str:
    """抹掉数字再归一化，用来判断两行是不是同一个页眉页脚。"""
    return _SPACES.sub(" ", _DIGITS.sub("", line)).strip().lower()


def _strip_invisible(text: str) -> str:
    """清掉零宽字符与不换行空格：它们肉眼不可见，却会跟着正文一起进向量。"""
    return _ZERO_WIDTH.sub("", text).replace("\u00a0", " ")


def _is_page_number(line: str, page: int | None) -> bool:
    """判断一行是不是页码：数字要与所在页的页号相差不超过 1。

    没有页号可对照时不判页码，正文里单独成行的年份不能被整行删掉。
    """
    if page is None:
        return False
    matched = _PAGE_NUMBER.fullmatch(line)
    return matched is not None and abs(int(matched.group(1)) - page) <= 1


def _from_pdf(text: str, repeated: set[str], page: int | None) -> str:
    """PDF 抽取的文本常混进页眉页脚、页码与被拆散的空格，逐行过滤。"""
    kept = []
    for raw in _strip_invisible(text).splitlines():
        line = _SPACES.sub(" ", raw).strip()
        if not line or _fingerprint(line) in repeated or _is_page_number(line, page):
            continue
        kept.append(line)
    return _join_hyphens(_BLANK_LINES.sub("\n\n", "\n".join(kept))).strip()


def _join_hyphens(text: str) -> str:
    """修复英文断词：折行处的连字符不是词的一部分。"""
    return _HYPHEN_KEEP.sub(r"\1-\2", _HYPHEN_JOIN.sub(r"\1\2", text))


def _normalize(text: str) -> str:
    """结构化文本只去行尾空白、合并连续空行：行内空格要留着，代码缩进不能被压掉。"""
    lines = [_strip_invisible(line).rstrip() for line in text.splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()
