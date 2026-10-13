"""评估入口：探活、逐层执行、报结果并与基线比对。

在 backend 目录下运行：
    uv run python -m evals.runner                     # 跑全部已实现的层
    uv run python -m evals.runner --layer retrieval   # 只跑一层
    uv run python -m evals.runner --update-baseline   # 把当前结果记成基线
退出码 0 通过，1 有用例未通过或相对基线退化，2 环境不可用。

执行顺序固定：探活 → 逐层跑 → 打印 → 比基线 → 定退出码。
探活放在最前面，是为了让「数据库 / 向量化服务没起」这类环境问题
一眼看出来，而不是让它表现成一堆用例失败。
"""

import argparse
import asyncio

import httpx
from sqlalchemy import text

from app.core.config import get_settings
from app.infrastructure.database.session import Database
from app.modules.knowledge.service import KnowledgeService
from evals import report as report_module
from evals import retrieval

# 已实现的层；新增一层就在这里登记，入口不用改。
# 键是层的 ascii 名（与 Report.key、基线文件的键一致），值是那一层的 async run()。
LAYERS = {"retrieval": retrieval.run}


async def _probe(settings, database) -> str | None:
    """执行前先探活：缺哪一项就直说，别让它表现成用例失败。

    两项检查，返回第一条失败原因，都通过时返回 None：
    - 数据库能不能连（跑一句 `SELECT 1`）；
    - 向量化服务在不在（打它的 `/api/tags`，这是检索的必要依赖）。
    """
    try:
        async with database.sessions() as session:
            await session.execute(text("SELECT 1"))
    except Exception as error:
        return f"数据库不可用：{type(error).__name__}: {error}"
    url = f"{settings.embedding_base_url.rstrip('/')}/api/tags"
    try:
        # trust_env=False：不读环境变量里的代理设置，直连本机向量化服务。
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            response = await client.get(url)
    except httpx.HTTPError as error:
        return f"向量化服务不可用（{url}）：{error}"
    # 200 才算活着：能连通但返回错误码时同样不能开始评估。
    if response.status_code != 200:
        return f"向量化服务返回 {response.status_code}（{url}）"
    return None


async def main(layer_names: list[str], update_baseline: bool) -> int:
    """跑指定层并打印报告，返回进程退出码。"""
    settings = get_settings()
    database = Database(settings.foundation_database_url or settings.database_url)
    try:
        problem = await _probe(settings, database)
        if problem:
            print(f"评估无法开始：{problem}")
            return 2
        # 各层共用同一个服务实例；层内部只读，跑完语料与向量都不变。
        service = KnowledgeService(database, settings)
        # 拿到各层的评估报告
        reports = [await LAYERS[name](service) for name in layer_names]
    finally:
        # 无论探活失败还是跑完，都要把连接池收干净。
        await database.close()

    # 读取基线
    baseline = report_module.load_baseline()
    regressions = 0
    for layer in reports:
        # 打印报告
        print("\n".join(report_module.render(layer)))
        # 与基线比
        lines, count = report_module.compare(layer, baseline.get(layer.key, {}))
        regressions += count
        print("\n".join(lines))

    # 汇总各层：known_gaps / blocked 只报数，不影响退出码；failed 与退化才影响。
    failed = sum(layer.failed for layer in reports)
    known_gaps = sum(layer.known_gaps for layer in reports)
    blocked = sum(layer.blocked for layer in reports)
    if update_baseline:
        print(f"\n基线已更新：{report_module.save_baseline(reports)}")
    notes = [f"通过 {sum(layer.passed for layer in reports)}/{sum(layer.evaluated for layer in reports)}"]
    if known_gaps:
        notes.append(f"已知缺口 {known_gaps} 条")
    if blocked:
        notes.append(f"语料缺失 {blocked} 条")
    if failed:
        notes.append(f"未通过 {failed} 条")
    if regressions:
        notes.append(f"相对基线退化 {regressions} 条")
    print("\n结论：" + "，".join(notes))
    return 1 if (failed or regressions) else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """只收层名与基线开关，其余交给用例文件。

    `--layer` 可重复传入（取值限定在 LAYERS 里），省略则跑全部已实现的层；
    `--update-baseline` 表示把本次结果记成基线，而不是与已有基线比对。
    """
    # 创建解析器 argparse用于解析命令行参数， ArgumentParser 参数解析器类 description 脚本的描述信息
    parser = argparse.ArgumentParser(description="跑评估用例并比对基线")
    # 添加--layer参数
    parser.add_argument(
        "--layer",
        action="append", # 可重复传入，多次指定会追加到列表
        choices=sorted(LAYERS), # 取值限定在 LAYERS 里
        help="只跑指定层，可重复；省略则跑全部已实现的层", # 帮助文本
    )
    parser.add_argument("--update-baseline", action="store_true", help="把本次结果记成新基线")
    return parser.parse_args(argv)


if __name__ == "__main__":
    # 未指定 --layer 时跑全部层；返回值交给 SystemExit 作为退出码，供 CI 判断。
    arguments = parse_args()
    raise SystemExit(asyncio.run(main(arguments.layer or sorted(LAYERS), arguments.update_baseline)))
