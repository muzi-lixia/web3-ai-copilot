"""资产子图回归：参数规范化、并发取数、部分失败与全失败的分支，不涉及模型与网络。"""

import json

import pytest
from langgraph.constants import CONFIG_KEY_CHECKPOINTER

from app.agent.assets import ASSETS_GRAPH
from app.agent.context import RunContext
from app.core.exceptions import UpstreamError


class FakeClient:
    """按调用参数返回结果或抛错，并记下每一次请求的 payload。"""

    default_chain_id = 80094

    def __init__(self, failures=()):
        self.calls = []
        self.failures = set(failures)

    async def request(self, method, path, token, **kwargs):
        payload = kwargs["json"]
        self.calls.append(payload)
        key = payload.get("symbol") or "all"
        if key in self.failures:
            raise UpstreamError(f"{key} 查询失败")
        return {
            "summary": {"result_id": f"r-{key}", "scope": payload.get("symbol") or "registered_tokens"},
            "result": {"result_id": f"r-{key}", "kind": "balance", "status": "complete", "body": {}},
        }


class FakeRepository:
    """只记录登记的引用，够断言归属写入即可。"""

    def __init__(self):
        self.refs = []

    async def add_ref(self, user, cid, result_id):
        self.refs.append((user, cid, result_id))


def make_context(failures=()):
    """构造一条完整的运行上下文：客户端的失败集合决定哪几条取不到数。"""
    client = FakeClient(failures)
    return RunContext("user-1", "conv-1", "token", client, FakeRepository()), client


async def run(context, **state):
    """按子图入口的调用方式跑一次，返回最终状态。"""
    return await ASSETS_GRAPH.ainvoke(
        {"currency": "USD", **state},
        context=context,
        config={"configurable": {CONFIG_KEY_CHECKPOINTER: None}},
    )


async def test_no_symbols_asks_for_the_overview():
    """不点币种时只发一次请求，且不带 symbol，由下游查遍登记网络。"""
    context, client = make_context()
    state = await run(context, symbols=[])

    assert client.calls == [{"chain_id": None, "currency": "USD"}]
    assert json.loads(state["reply"])["results"] == [
        {"result_id": "r-all", "scope": "registered_tokens"}
    ]


async def test_named_symbols_are_normalized_and_queried_separately():
    """符号统一大写、合约地址保持原样，各自发一条请求。"""
    context, client = make_context()
    await run(context, symbols=[" bera", "0xAbCd"], chain_id=1)

    assert [(c["symbol"], c["chain_id"]) for c in client.calls] == [("BERA", 1), ("0xAbCd", 1)]


async def test_named_symbols_fall_back_to_the_deployed_chain():
    """点名币种但没给网络时用部署默认网络，不是「全部网络」。"""
    context, client = make_context()
    await run(context, symbols=["HONEY"])

    assert client.calls[0]["chain_id"] == 80094


async def test_results_are_registered_and_shown():
    """每条结果都登记归属并进入本轮展示通道。"""
    context, client = make_context()
    await run(context, symbols=["BERA", "HONEY"])

    assert context.repository.refs == [
        ("user-1", "conv-1", "r-BERA"),
        ("user-1", "conv-1", "r-HONEY"),
    ]
    assert {item["result_id"] for item in context.results} == {"r-BERA", "r-HONEY"}


async def test_one_failed_symbol_keeps_the_others():
    """一条取不到数不影响其他条，失败项连同原因交给模型。"""
    context, _ = make_context(failures=["HONEY"])
    state = await run(context, symbols=["BERA", "HONEY"])

    reply = json.loads(state["reply"])
    assert [item["result_id"] for item in reply["results"]] == ["r-BERA"]
    assert reply["failed"] == [{"symbol": "HONEY", "reason": "HONEY 查询失败"}]


async def test_all_failed_raises_the_original_error():
    """一条都没成功时抛出原始业务异常，错误码不被包装掉。"""
    context, _ = make_context(failures=["all"])
    with pytest.raises(UpstreamError):
        await run(context, symbols=[])
