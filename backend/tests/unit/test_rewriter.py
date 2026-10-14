"""检索改写的前置判据与短路：问题里没有指代词时，连模型都不该调。"""

from types import SimpleNamespace

import pytest

from app.agent.rag.rewriter import fallback, needs_rewrite, rewrite


class FakeModel:
    """只回一句改写结果，并记下被调用过几次。"""

    model_name = "fake"

    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def model_copy(self, update):
        return self

    def with_structured_output(self, schema):
        return self

    async def ainvoke(self, prompt):
        self.calls += 1
        return SimpleNamespace(query=self.answer)


HISTORY = "用户：质押 BERA 会得到什么\n助手：会得到 sWBERA。"


@pytest.mark.parametrize(
    "query",
    [
        "那它的解锁期呢",
        "这个机制怎么运作",
        "上面说的规则是什么",
        "它的原生币是什么",
    ],
)
def test_needs_rewrite_hits_pronouns(query):
    assert needs_rewrite(query)


@pytest.mark.parametrize(
    "query",
    [
        "BeaconKit 是什么",
        "sWBERA 的解锁周期",
        "我该怎么操作",
        "其他链的手续费是多少",
    ],
)
def test_needs_rewrite_ignores_standalone(query):
    assert not needs_rewrite(query)


async def test_no_history_skips_model():
    model = FakeModel("不该被调用")
    result = await rewrite(model, "那它的解锁期呢", "")
    assert model.calls == 0
    assert result == fallback("那它的解锁期呢")


async def test_standalone_query_skips_model():
    model = FakeModel("不该被调用")
    result = await rewrite(model, "BeaconKit 是什么", HISTORY)
    assert model.calls == 0
    assert result == fallback("BeaconKit 是什么")


async def test_pronoun_with_history_calls_model():
    model = FakeModel("sWBERA 的解锁期")
    result = await rewrite(model, "那它的解锁期呢", HISTORY)
    assert model.calls == 1
    assert result.query == "sWBERA 的解锁期"
    assert result.rewritten is True


async def test_unchanged_answer_is_not_rewritten():
    model = FakeModel("那它的解锁期呢")
    result = await rewrite(model, "那它的解锁期呢", HISTORY)
    assert model.calls == 1
    assert result.rewritten is False
