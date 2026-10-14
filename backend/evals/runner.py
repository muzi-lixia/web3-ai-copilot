"""评估入口：探活、逐层执行、报结果并与基线比对。

在 backend 目录下运行：
    uv run python -m evals.runner                     # 跑全部已实现的层
    uv run python -m evals.runner --layer retrieval   # 只跑一层
    uv run python -m evals.runner --update-baseline   # 把当前结果记成基线
退出码 0 通过，1 有用例未通过或相对基线退化，2 环境不可用。

执行顺序固定：探活 → 逐层跑 → 打印 → 比基线 → 定退出码。
探活按这次要跑的层检查依赖，放在最前面，是为了让「数据库 / 向量化服务 / 模型没起」
这类环境问题一眼看出来，而不是让它表现成一堆用例失败。
"""

import argparse
import asyncio

import httpx
from sqlalchemy import text

from app.agent.models import ENDPOINTS, select_settings
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.infrastructure.database.session import Database
from app.modules.knowledge.service import KnowledgeService
from evals import report as report_module
from evals import retrieval, rewrite, routing

# 已实现的层；新增一层就在这里登记，入口不用改。
# 键是层的 ascii 名（与 Report.key、基线文件的键一致），值是那一层的 async run()。
LAYERS = {"retrieval": retrieval.run, "rewrite": rewrite.run, "routing": routing.run}


async def _probe_database(database) -> str | None:
    """数据库能不能连：检索层要读语料，连不上后面全是失败。"""
    try:
        async with database.sessions() as session:
            await session.execute(text("SELECT 1"))
    except Exception as error:
        return f"数据库不可用：{type(error).__name__}: {error}"
    return None


async def _probe_embedding(settings) -> str | None:
    """向量化服务在不在：检索的必要依赖，打它的 `/api/tags`。

    `trust_env=False`：不读环境变量里的代理设置，直连本机向量化服务。
    200 才算活着：能连通但返回错误码时同样不能开始评估。
    """
    url = f"{settings.embedding_base_url.rstrip('/')}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            response = await client.get(url)
    except httpx.HTTPError as error:
        return f"向量化服务不可用（{url}）：{error}"
    if response.status_code != 200:
        return f"向量化服务返回 {response.status_code}（{url}）"
    return None


async def _probe_model(settings, provider) -> str | None:
    """模型服务在不在，以及这一层要用的那个模型在不在。

    `provider` 为空表示部署配置的主模型。本地服务打 `/api/tags` 看模型清单，
    云端打 `/models`（用配置里的密钥鉴权）—— 两者都只列清单，不产生生成费用。
    `trust_env=False`：不读环境变量里的代理设置，免得探活被代理挡住而模型本身可用。
    """
    try:
        chosen = select_settings(settings, provider)
    except AppError as error:
        return f"模型配置不可用：{error}"
    base = (chosen.model_base_url or ENDPOINTS[chosen.model_provider]).rstrip("/")
    if chosen.model_provider == "ollama":
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                response = await client.get(f"{base}/api/tags")
        except Exception as error:
            return f"模型服务不可用（{base}）：{type(error).__name__}: {error}"
        if response.status_code != 200:
            return f"模型服务返回 {response.status_code}（{base}）"
        names = {item.get("name", "") for item in response.json().get("models", [])}
        if chosen.model_name not in names:
            return f"本地没有要用的模型（{base} 缺 {chosen.model_name}）"
        return None
    headers = {"Authorization": f"Bearer {chosen.model_api_key.get_secret_value()}"}
    try:
        async with httpx.AsyncClient(
            timeout=10.0, trust_env=False, headers=headers, proxy=chosen.model_proxy or None
        ) as client:
            response = await client.get(f"{base}/models")
    except Exception as error:
        return f"模型服务不可用（{base}）：{type(error).__name__}: {error}"
    if response.status_code != 200:
        return f"模型服务返回 {response.status_code}（{base}）"
    names = {item.get("id", "") for item in response.json().get("data", [])}
    if chosen.model_name not in names:
        return f"模型清单里没有 {chosen.model_name}（{base}）"
    return None


async def _probe(settings, database, layer_names) -> str | None:
    """执行前按这次要跑的层探活：缺哪一项就直说，别让它表现成用例失败。

    只检查真会跑到的依赖，逐项返回第一条失败原因，都通过时返回 None：
    - 检索层：数据库能连、向量化服务在；
    - 改写层：它固定要用的本地改写模型在；
    - 路由层：部署配置的主模型在（只列清单，不调生成）。
    """
    if "retrieval" in layer_names:
        problem = await _probe_database(database) or await _probe_embedding(settings)
        if problem:
            return problem
    if "rewrite" in layer_names:
        problem = await _probe_model(settings, rewrite.PROVIDER)
        if problem:
            return problem
    if "routing" in layer_names:
        problem = await _probe_model(settings, None)
        if problem:
            return problem
    return None


async def main(layer_names: list[str], update_baseline: bool) -> int:
    """跑指定层并打印报告，返回进程退出码。"""
    settings = get_settings()
    database = Database(settings.foundation_database_url or settings.database_url)
    try:
        problem = await _probe(settings, database, layer_names)
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
