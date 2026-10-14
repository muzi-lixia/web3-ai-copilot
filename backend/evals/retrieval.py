"""L3 检索层：真问题跑一遍线上检索，看该取的片段有没有进前 k 名。

只调向量化与检索，不调生成模型；断言的是召回位置，不是最终回答的措辞。

每条用例有三种结果：
- `ok`      期望的片段进了前 k 名，判定通过；
- `fail`    跑完了但没进前 k 名，是否算「已知缺口」由用例上的标记决定；
- `blocked` 期望的语料根本不在库里，跳过判定 —— 否则缺数据会被误读成召回退化。
"""

import math

from app.modules.knowledge.service import KnowledgeService
from app.modules.knowledge.version import embedding_version
from evals.cases import RetrievalCase, load_retrieval_cases
from evals.report import Report

# 三种判定结果。用字符串常量而不是布尔值，是为了让 blocked 与 fail 可区分。
OK, FAIL, BLOCKED = "ok", "fail", "blocked"

# 报告里报的分档档位；超过判定范围的档位会量到用例根本没取回来的名次，所以不报。
RECALL_STEPS = (1, 3, 5)


def _matches(case: RetrievalCase, hit: dict) -> bool:
    """判断检索结果是否命中用例的期望。

     命中需要同时满足两个条件（必须落在同一个片段上）：
    1. 出处匹配：片段的 filename 或 title 与用例的 expect_doc 一致
    2. 内容匹配：片段正文包含 expect_any 中的任意一条原文（expect_any 为空则跳过）

    为什么必须同一个片段？
    - 如果片段A出处对、片段B内容对，等于什么都没查到
    - 用户需要的是"一个片段同时给出出处和答案"
    """
    if case.expect_doc and case.expect_doc not in (hit["filename"], hit["title"]):
        return False
    return not case.expect_any or any(text in hit["content"] for text in case.expect_any)


def _brief(hits: list[dict]) -> str:
    """失败时要说清「实际取回了什么」，只列名次与出处，正文不进报告。

    每行形如 `1. 0.6625 bera.pdf #3 标题`：名次、相似度（4 位小数）、
    文件名、片段序号、所属标题（空标题显示为「（无标题）」）。
    只给定位信息，不打印整段正文，免得报告被内容淹没。
    """
    return "\n".join(
        f"        {index}. {hit['score']:.4f} {hit['filename']} #{hit['ordinal']} "
        f"{hit['heading'] or '（无标题）'}"
        for index, hit in enumerate(hits, start=1)
    )


def _recall_at(evaluated: list[dict], k: int) -> float:
    """前 k 名内命中的条数占可判定条数的比例，判据与通过率完全一样。

    分母是可判定条数（不含语料缺失的），所以 k 取 5 时这个数就等于通过率，
    报出来只是让它按档位列清楚。
    """
    if not evaluated:
        return 0.0
    return sum(1 for item in evaluated if item["rank"] and item["rank"] <= k) / len(evaluated)


def _ndcg_at(evaluated: list[dict], k: int) -> float:
    """前 k 名内的归一化折损累计增益；每条用例只有一个相关片段，理想情况取第 1 名。

    单相关时理想折损为 1，于是每条的增益就是 1/log2(名次+1)：第 1 名得 1.0、
    第 2 名得 0.63、第 3 名得 0.50 —— 比 MRR 的 1/名次衰减慢，
    用来把「排在前列」和「排在最前」分成两个水平。
    """
    if not evaluated:
        return 0.0
    return sum(
        1 / math.log2(item["rank"] + 1) for item in evaluated if item["rank"] and item["rank"] <= k
    ) / len(evaluated)


async def _evaluate(cases: list[RetrievalCase], service: KnowledgeService, corpus: set[str]):
    """逐条检索并判定；语料不在库中的记为 blocked，免得把缺数据当成召回退化。

    - 只声明了 `expect_doc` 而库里没有这份文档 → 直接 blocked，连检索都不跑；
    - 其余情况都真跑一次 `service.search`，取第一条满足判据的名次；
    - `rank` 为 None 表示前 k 名里没有满足判据的片段。
    结果按用例顺序返回，报告里的明细也按这个顺序排。
    """
    outcomes: list[dict] = []
    for case in cases:
        # 期望的文档不在库里时，检索再准也不可能命中，判失败是误判。
        if (
            (case.expect_doc and case.expect_doc not in corpus)
            or any(doc not in corpus for doc in case.requires_docs)
            or (case.expect_empty and not corpus)
        ):
            outcomes.append({"case": case, "status": BLOCKED, "rank": None, "hits": []})
            continue
        # 执行检索并拿到检索返回的结果
        hits = (await service.search(case.query, case.top_k))["hits"]
        # enumerate 从 1 开始：名次是给人看的，第 1 名就是第 1 名。
        rank = next((index for index, hit in enumerate(hits, start=1) if _matches(case, hit)), None)
        if case.expect_empty:
            # 仅把判定编码为 1/None 供基线比较，不计入正样本排名指标。
            rank = 1 if not hits else None
        status = OK if rank else FAIL
        outcomes.append({"case": case, "status": status, "rank": rank, "hits": hits})
    return outcomes


def _problems(outcomes: list[dict]) -> list[str]:
    """把未通过、已知缺口与缺失各排一段；缺语料要单独说，否则会被读成召回变差。

    四类分开排是有意的：
    - 未通过：真问题，附上实际取回的名次表，方便判断差多远；
    - 已知缺口：期望对的，只是现在取不到，附上 note 里写的原因；
    - 已修复的缺口：`expected_fail` 还挂着但已经命中，提醒去掉这个标记；
    - 语料缺失：库里的数据问题，不是召回问题。
    只返回有内容的那几段，全部正常时返回空列表。
    """
    failed = [item for item in outcomes if item["status"] == FAIL and not item["case"].expected_fail]
    gaps = [item for item in outcomes if item["status"] == FAIL and item["case"].expected_fail]
    fixed = [item for item in outcomes if item["status"] == OK and item["case"].expected_fail]
    blocked = [item for item in outcomes if item["status"] == BLOCKED]
    lines: list[str] = []
    if failed:
        lines.append(f"\n  未通过 {len(failed)} 条：")
        for item in failed:
            case = item["case"]
            # 只校验出处时显式写出来，避免被读成「这条用例没有任何期望」。
            wanted = "、".join(f'"{text}"' for text in case.expect_any) or "（只校验出处）"
            lines.append(f"    ✗ {case.name}  「{case.query}」")
            lines.append(
                "        期望无相关片段，实际却返回了片段"
                if case.expect_empty
                else f"        期望 {case.expect_doc or '任一文档'} 命中 {wanted}"
            )
            lines.append(_brief(item["hits"]))
    if gaps:
        lines.append(f"\n  已知缺口 {len(gaps)} 条（不计入失败）：")
        for item in gaps:
            case = item["case"]
            lines.append(f"    ○ {case.name}  「{case.query}」期望命中 {case.expect_any}")
            # 取不到的原因写在用例文件的 note 里，这里原样带出来。
            if case.note:
                lines.append(f"        {case.note}")
            lines.append(_brief(item["hits"]))
    if fixed:
        # 缺口被填上了，标记就该撤掉；留着会掩盖真实的召回退化。
        lines.append("\n  以下用例已能命中，请从用例文件里去掉 expected_fail：")
        lines.extend(f"    • {item['case'].name}（现为第 {item['rank']} 名）" for item in fixed)
    if blocked:
        lines.append(f"\n  语料缺失 {len(blocked)} 条（不计入通过率）：")
        for item in blocked:
            case = item["case"]
            lines.append(f"    - {case.name}  所需语料未就绪或向量版本不兼容")
    return lines


async def run(service: KnowledgeService) -> Report:
    """跑完检索层全部用例，返回可比较的结果与可读的失败说明。

    语料集合由已就绪且向量版本兼容的文档的 `filename` 与 `title` 一起拼成，
    用来判断一条用例是不是因为「文档不在库」而 blocked。
    headline 只报已就绪的文档数与片段数，说明这次评估是在多大的语料上做的。
    """
    # 读用例文件
    cases = load_retrieval_cases()
    # 所有文档的元数据列表
    documents = await service.listing()
    # 只有 ready 的文档才算语料；pending/failed 的既没有分片也不该进统计口径。
    version = embedding_version(service.settings)
    ready = [
        document
        for document in documents
        if document["status"] == "ready" and document.get("embedding_version") == version
    ]
    corpus = {document["filename"] for document in ready} | {document["title"] for document in ready}

    outcomes = await _evaluate(cases, service, corpus)
    # 1. 过滤掉 blocked（语料缺失）
    evaluated = [item for item in outcomes if item["status"] != BLOCKED]
    # 2. 提取名次（用于基线比对
    ranks = {item["case"].name: item["rank"] for item in evaluated}
    # 3. 统计通过
    passed = [item for item in evaluated if item["status"] == OK]
    # 4. MRR 仅计算有答案的正例；无答案负例独立统计，不虚增召回指标。
    positive = [item for item in evaluated if not item["case"].expect_empty]
    negative = [item for item in evaluated if item["case"].expect_empty]
    mrr = sum(1 / item["rank"] for item in positive if item["rank"]) / len(positive) if positive else 0.0
    # 5. 分档召回与名次折损：档位跟着用例声明的 top_k 走，超出它就会量到没取回来的名次
    top_k = max(case.top_k for case in cases)
    metrics = {"MRR": mrr}
    metrics.update({f"Recall@{step}": _recall_at(positive, step) for step in RECALL_STEPS if step <= top_k})
    metrics[f"nDCG@{top_k}"] = _ndcg_at(positive, top_k)

    if negative:
        accuracy = sum(item["status"] == OK for item in negative) / len(negative)
        metrics["NoAnswerAccuracy"] = accuracy
        metrics["FalseAcceptanceRate"] = 1 - accuracy
    if positive:
        metrics["AnswerableEmptyRate"] = sum(not item["hits"] for item in positive) / len(positive)

    return Report(
        key="retrieval",
        title="检索层 L3",
        headline=f"语料 {len(ready)} 份文档 / {sum(d['chunk_count'] for d in ready)} 个片段",
        # 通过数
        passed=len(passed),
        # 可评估数（不含 bloc
        evaluated=len(evaluated),
        # 只有「未命中的真问题」算失败；已知缺口单独计数，不影响退出码。失败数
        failed=len([item for item in evaluated if item["status"] == FAIL and not item["case"].expected_fail]),
        # 已知缺口数
        known_gaps=len([item for item in evaluated if item["status"] == FAIL and item["case"].expected_fail]),
        # 跳过的条数 = 全部结果减去可判定的那部分。
        blocked=len(outcomes) - len(evaluated),
        # 评估指标
        metrics=metrics,
        # 明细文字交给本层自己拼，报告模块只负责按行打印。
        problems=_problems(outcomes),
        # 名次字典
        ranks=ranks,
    )
