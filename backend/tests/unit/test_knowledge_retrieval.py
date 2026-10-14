"""线上检索的过滤、向量版本与无答案评测语义。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import ConflictError
from app.foundation.config import FoundationSettings
from app.modules.knowledge.service import KnowledgeService
from app.modules.knowledge.version import embedding_version
from evals import retrieval
from evals.cases import RetrievalCase, load_retrieval_cases
from evals.report import compare


def hit(score, content="质押规则", **extra):
    return dict(
        score=score, content=content, filename="rules.md", title="规则", heading="规则", ordinal=0, **extra
    )


@pytest.fixture
def service(monkeypatch):
    settings = FoundationSettings(_env_file=None)
    value = KnowledgeService(SimpleNamespace(), settings)
    value.repository = SimpleNamespace(
        index_status=AsyncMock(return_value={"ready": 2, "compatible": 1}),
        search=AsyncMock(return_value=[]),
        listing=AsyncMock(),
        read=AsyncMock(),
    )
    monkeypatch.setattr("app.modules.knowledge.service.embed", AsyncMock(return_value=[[1, 0, 0]]))
    return value


async def test_irrelevant_nearest_neighbors_produce_no_answer(service):
    service.repository.search.return_value = [hit(0.4), hit(0.54999)]
    result = await service.search("其他规则", 5)
    assert result["hits"] == []
    assert result["status"] == "no_answer"
    assert result["reason"] == "below_threshold"
    assert result["excluded_documents"] == 1


async def test_filter_uses_absolute_score_before_ranking_and_keeps_boundary(service):
    service.repository.search.return_value = [hit(0.9), hit(0.55), hit(0.54999), hit(float("nan"))]
    result = await service.search("质押规则", 5)
    assert {row["score"] for row in result["hits"]} == {0.9, 0.55}
    assert result["status"] == "matched"
    args = service.repository.search.await_args.args
    assert args[1:] == (20, embedding_version(service.settings))


async def test_limit_applies_after_filter_and_rerank(service):
    service.repository.search.return_value = [hit(0.9, "不相关的句子"), hit(0.89, "质押规则")]
    result = await service.search("质押规则", 1)
    assert len(result["hits"]) == 1
    assert service.repository.search.await_args.args[1] == 20


async def test_version_mismatch_is_not_reported_as_no_answer(service):
    service.repository.index_status.return_value = {"ready": 2, "compatible": 0}
    with pytest.raises(ConflictError) as exc:
        await service.search("规则", 5)
    assert exc.value.code == "embedding_version_mismatch"
    service.repository.search.assert_not_awaited()


async def test_empty_index_returns_no_answer_without_embedding(service, monkeypatch):
    embed = AsyncMock()
    monkeypatch.setattr("app.modules.knowledge.service.embed", embed)
    service.repository.index_status.return_value = {"ready": 0, "compatible": 0}
    result = await service.search("规则", 5)
    assert result["reason"] == "empty_index"
    embed.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value",
    [
        ("embedding_model", "other-model"),
        ("embedding_revision", "2"),
        ("knowledge_embedding_dimensions", 512),
    ],
)
def test_each_vector_space_component_changes_version(field, value):
    settings = FoundationSettings(_env_file=None)
    changed = settings.model_copy(update={field: value})
    assert embedding_version(settings) != embedding_version(changed)
    assert embedding_version(settings) == embedding_version(settings.model_copy())


async def test_unknown_legacy_document_is_marked_for_reindex(service):
    service.repository.listing.return_value = [
        {"status": "ready", "embedding_version": None},
        {"status": "ready", "embedding_version": embedding_version(service.settings)},
    ]
    assert [row["index_compatible"] for row in await service.listing()] == [False, True]


def empty_case():
    return RetrievalCase(
        name="empty",
        query="怎么做寿司",
        expect_doc=None,
        expect_any=(),
        top_k=5,
        expect_empty=True,
        requires_docs=("rules.md",),
    )


async def test_negative_eval_requires_real_corpus_and_rejects_any_hit():
    value = SimpleNamespace(search=AsyncMock(return_value={"hits": []}))
    outcomes = await retrieval._evaluate([empty_case()], value, set())
    assert outcomes[0]["status"] == retrieval.BLOCKED
    value.search.assert_not_awaited()
    outcomes = await retrieval._evaluate([empty_case()], value, {"rules.md"})
    assert outcomes[0]["status"] == retrieval.OK
    value.search.return_value = {"hits": [hit(0.8)]}
    outcomes = await retrieval._evaluate([empty_case()], value, {"rules.md"})
    assert outcomes[0]["status"] == retrieval.FAIL


async def test_negative_cases_do_not_inflate_positive_ranking_metrics(service, monkeypatch):
    positive = RetrievalCase("answer", "质押规则", "rules.md", (), 5)
    monkeypatch.setattr(retrieval, "load_retrieval_cases", lambda: [positive, empty_case()])
    service.repository.listing.return_value = [
        dict(
            filename="rules.md",
            title="规则",
            status="ready",
            chunk_count=1,
            embedding_version=embedding_version(service.settings),
        )
    ]
    service.search = AsyncMock(side_effect=[{"hits": [hit(0.8)]}, {"hits": []}])
    report = await retrieval.run(service)
    assert report.metrics["MRR"] == 1
    assert report.metrics["NoAnswerAccuracy"] == 1
    assert report.metrics["FalseAcceptanceRate"] == 0
    assert report.passed == 2
    report.ranks["empty"] = None
    assert compare(report, {"ranks": {"empty": 1}})[1] == 1


def test_negative_case_loader_rejects_conflicting_expectations(tmp_path):
    path = tmp_path / "cases.toml"
    path.write_text('[[case]]\nname="bad"\nquery="q"\nexpect_empty=true\nexpect_doc="rules.md"\n')
    with pytest.raises(ValueError, match="不能同时"):
        load_retrieval_cases(path)
    assert sum(case.expect_empty for case in load_retrieval_cases()) == 3


async def test_search_tool_explicitly_instructs_no_answer_when_filtered_empty():
    import json

    from app.agent.rag.rewriter import RewriteResult
    from app.agent.tools.knowledge import search_docs

    context = SimpleNamespace(
        token="permit",
        results=[],
        rewrite=AsyncMock(return_value=RewriteResult(query="无关问题", rewritten=False)),
        client=SimpleNamespace(
            request=AsyncMock(return_value={"query": "无关问题", "hits": [], "status": "no_answer"})
        ),
    )
    runtime = SimpleNamespace(context=context, state={"messages": []})
    result = json.loads(await search_docs.coroutine("无关问题", runtime))
    assert result["status"] == "no_answer"
    assert "不要猜测" in result["note"]
    assert context.results[0]["body"]["hits"] == []


async def test_all_incompatible_corpus_blocks_eval_instead_of_passing_negatives(service, monkeypatch):
    monkeypatch.setattr(retrieval, "load_retrieval_cases", lambda: [empty_case()])
    service.repository.listing.return_value = [
        dict(filename="rules.md", title="规则", status="ready", chunk_count=1, embedding_version="old")
    ]
    service.search = AsyncMock()
    report = await retrieval.run(service)
    assert report.blocked == 1
    assert report.evaluated == 0
    assert "NoAnswerAccuracy" not in report.metrics
    service.search.assert_not_awaited()


async def test_online_embedding_concurrency_budget_limits_work(monkeypatch):
    import asyncio

    settings = FoundationSettings(_env_file=None, embedding_query_concurrency=1)
    service = KnowledgeService(SimpleNamespace(), settings)
    service.repository = SimpleNamespace(
        index_status=AsyncMock(return_value={"ready": 1, "compatible": 1}), search=AsyncMock(return_value=[])
    )
    active = peak = 0

    async def embed(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        assert kwargs["batch_size"] == 1
        assert kwargs["timeout"] == settings.embedding_query_timeout_seconds
        return [[1, 0, 0]]

    monkeypatch.setattr("app.modules.knowledge.service.embed", embed)
    await asyncio.gather(service.search("first", 5), service.search("second", 5))
    assert peak == 1


async def test_online_embedding_timeout_is_bounded(monkeypatch):
    import asyncio

    from app.core.exceptions import UpstreamError

    settings = FoundationSettings(_env_file=None, embedding_query_timeout_seconds=0.01)
    service = KnowledgeService(SimpleNamespace(), settings)
    service.repository = SimpleNamespace(index_status=AsyncMock(return_value={"ready": 1, "compatible": 1}))

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("app.modules.knowledge.service.embed", blocked)
    with pytest.raises(UpstreamError, match="等待超时"):
        await service.search("q", 5)
