"""结果呈现与基线比对：各层产出的报告统一打印，并指出相对基线的升降。

本模块不认识任何具体层：它只处理 `Report` 这个通用结构，
所以新增一层不用改这里，只要那一层的 `run()` 返回一个 `Report`。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

# 基线文件的默认位置，与本模块同级；不存在不是错误，第一次跑就是没有。
BASELINE_PATH = Path(__file__).with_name("baseline.json")


@dataclass
class Report:
    """一层的执行结果。

    字段：
    - `key`        层的 ascii 名，基线文件与 `--layer` 都用它，必须与登记处一致。
    - `title`      打印用的中文层名。
    - `headline`   一行环境说明（如语料规模），说明结果是在什么条件下得到的。
    - `passed`     判定通过的用例数。
    - `evaluated`  可判定的用例数（不含 blocked），通过率的分母。
    - `failed`     未通过且不是已知缺口的用例数，唯一影响退出码的计数。
    - `known_gaps` 未通过但已标为已知缺口的用例数。
    - `blocked`    因语料缺失而跳过的用例数。
    - `metrics`    数值指标（如 MRR），键名固定；基线比对时逐个比数值。
    - `problems`   已格式化好的明细文字，由各层自己拼，本模块只负责打印。
    - `ranks`      用例名 → 名次；只收可判定的用例（语料缺失的既不算通过也不算失败），
                   基线比对靠它逐条看升降。
    """

    key: str
    title: str
    headline: str
    passed: int
    evaluated: int
    failed: int = 0
    known_gaps: int = 0
    blocked: int = 0
    metrics: dict[str, float] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    ranks: dict[str, int | None] = field(default_factory=dict)


def render(report: Report) -> list[str]:
    """排成可读的几行；失败、已知缺口与缺失明细由各层格式化好放进 problems。

    第 1 行「层名｜环境说明」，第 2 行通过数与指标，
    之后依次是语料缺失提示与 problems 里的明细。
    返回的是行列表，打印交给调用方。
    """
    summary = f"  通过 {report.passed}/{report.evaluated}"
    # 指标只在有值时拼进来，避免出现「MRR 0.00」这种没有意义的占位。
    if report.metrics:
        summary += "    " + "    ".join(f"{key} {value:.2f}" for key, value in report.metrics.items())
    if report.known_gaps:
        summary += f"    已知缺口 {report.known_gaps}"
    lines = [f"{report.title}｜{report.headline}", summary]
    if report.blocked:
        lines.append(f"  语料缺失跳过 {report.blocked} 条")
    lines.extend(report.problems)
    return lines


def load_baseline(path: Path | None = None) -> dict:
    """读基线；没有基线不是错误，第一次跑就是没有。

    返回原样的 JSON 结构：顶层是「层名 → {passed, evaluated, metrics, ranks}」。
    文件不存在时返回空字典，比对侧会把它当成「无记录」而不是退化。
    """
    source = path or BASELINE_PATH
    if not source.exists():
        return {}
    return json.loads(source.read_text(encoding="utf-8"))


def save_baseline(reports: list[Report], path: Path | None = None) -> Path:
    """把本次结果记成基线，键固定为层的 ascii 名。

    只落「下次比对用得上的字段」：通过数、可判定数、指标与逐条名次。
    明细文字不落盘 —— 它每次都会重新算出来，写进基线只会随时间失真。
    `ensure_ascii=False` 让中文值直接可读，末尾补一个换行方便看 diff。
    """
    target = path or BASELINE_PATH
    data = {
        report.key: {
            "passed": report.passed,
            "evaluated": report.evaluated,
            "metrics": report.metrics,
            "ranks": report.ranks,
        }
        for report in reports
    }
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def compare(report: Report, previous: dict) -> tuple[list[str], int]:
    """与基线逐条比对，返回可读的差异行与退化条数。

    判据只看「该取的片段排到第几名」：从命中变成未命中、或名次变差都算退化，
    其余变化只在报告里说一声，不影响退出码。

    两个约定：
    - 基线里没有这条用例 → 记 `absent`，算「新增」不算退化（用例是新加的）；
    - 名次里的 `None` 表示未命中，所以 `before is not None and rank is None` 即退化。
    指标变化单独拼在前面，阈值 1e-9 是为了滤掉浮点噪声、只报真实变化。
    """
    # 没有基线（首次运行）不是失败，提示一句怎么建立基线即可。
    if not previous:
        return ["  基线：无记录（用 --update-baseline 记下当前结果）"], 0
    lines: list[str] = []
    regressions = 0
    for name, rank in report.ranks.items():
        # "absent" 是「基线里没有」的哨兵值，必须与 None（未命中）区分开。
        before = previous.get("ranks", {}).get(name, "absent")
        if before == "absent":
            lines.append(f"  新增 {name}：第 {rank} 名" if rank else f"  新增 {name}：未命中")
        elif before is not None and rank is None:
            regressions += 1
            lines.append(f"  退化 {name}：第 {before} 名 → 未命中")
        elif before is not None and rank is not None and rank > before:
            # 名次数字变大 = 排得更靠后 = 退化。
            regressions += 1
            lines.append(f"  退化 {name}：第 {before} 名 → 第 {rank} 名")
        elif before is not None and rank is not None and rank < before:
            lines.append(f"  改善 {name}：第 {before} 名 → 第 {rank} 名")
        elif before is None and rank is not None:
            lines.append(f"  改善 {name}：未命中 → 第 {rank} 名")
    # 数值指标只在两边都有、且差异超过浮点噪声时才报。
    metric_lines = [
        f"  {key} {previous['metrics'][key]:.2f} → {value:.2f}"
        for key, value in report.metrics.items()
        if key in previous.get("metrics", {}) and abs(previous["metrics"][key] - value) > 1e-9
    ]
    # 指标差异排在逐条差异前面，先看整体水平再看具体哪几条动了。
    lines = metric_lines + lines
    if not lines:
        lines = ["  基线对比：逐条无变化"]
    return lines, regressions
