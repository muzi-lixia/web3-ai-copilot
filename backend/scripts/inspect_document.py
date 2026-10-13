"""离线检查一份原件经加载与清洗后剩下什么。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.modules.knowledge import cleaner, loader  # noqa: E402


def main(path: Path) -> None:
    """打印加载出的块、清洗后剩下的块，以及清洗过程中消失的行。"""
    blocks = loader.load(path.suffix.lower(), path.read_bytes())
    for block in blocks:
        print(block)
    cleaned = cleaner.clean(blocks)
    print(f"{path.name}：加载 {len(blocks)} 块 → 清洗后 {len(cleaned)} 块")

    print("\n加载结果")
    for block in blocks:
        first = block.text.splitlines()[0][:70] if block.text else ""
        print(f"  [{block.kind}/{block.source}/p{block.page}] {len(block.text):>5} 字  {first!r}")

    before = [line.strip() for block in blocks for line in block.text.splitlines()]
    after = {line.strip() for block in cleaned for line in block.text.splitlines()}
    removed = list(dict.fromkeys(line for line in before if line and line not in after))
    print(f"\n清洗中消失的行（{len(removed)}）")
    for line in removed:
        print(f"  {line!r}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
