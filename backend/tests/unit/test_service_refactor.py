"""新服务边界回归：输入隐私、标准工具、准确汇总和 JSON 日志。"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.context import RunContext
from app.agent.privacy import normalize_query
from app.agent.tools import TOOLS
from app.core.logging import JsonFormatter, TraceMiddleware, span, span_id, trace_id
from app.foundation.results import ResultService, summary


@pytest.mark.parametrize(
    "query",
    [
        "我有10个BERA，页面显示8个",
        "我的BERA余额是十万元",
        "我有1e18 BERA",
        "钱包余额$500，查询BERA",
    ],
)
def test_private_numbers_never_enter_model(query):
    safe = normalize_query(query)
    assert safe.text != query
    assert "10" not in safe.text and "500" not in safe.text and "十万" not in safe.text
    assert "BERA" in safe.text


def test_unknown_wallet_is_not_allowed_as_token():
    safe = normalize_query("查询合约0x" + "1" * 40 + "余额")
    assert safe.clarification is None and "0x" not in safe.text
    assert safe.text == "查询合约[地址已隐藏]余额"


def test_tools_only_expose_business_arguments():
    for tool in TOOLS:
        properties = tool.tool_call_schema.model_json_schema()["properties"]
        assert not set(properties) & {"runtime", "token", "user_id", "owner", "address"}


def test_log_drops_credentials_exception_and_raw_query():
    record = logging.LogRecord("test", logging.ERROR, "", 1, "wallet %s", ("0x" + "1" * 40,), None)
    record.token = "secret-token"
    record.answer = "123.456"
    record.duration_ms = 420
    body = JsonFormatter().format(record)
    assert "secret-token" not in body and "0x" not in body and "123.456" not in body
    assert json.loads(body)["duration_ms"] == 420


def test_nested_spans_restore_parent(caplog):
    root = trace_id.set("a" * 32)
    try:
        with span("turn.completed"):
            outer = span_id.get()
            with span("tool.completed"):
                assert span_id.get() != outer
            assert span_id.get() == outer
        assert span_id.get() == ""
    finally:
        trace_id.reset(root)


async def test_trace_middleware_preserves_stream_and_rejects_invalid_trace():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"event: done\n\n"})

    packets = []

    async def send(packet):
        packets.append(packet)

    await TraceMiddleware(app)(
        {"type": "http", "method": "GET", "headers": [(b"x-trace-id", b"Bearer secret")]}, None, send
    )
    assert len(dict(packets[0]["headers"])[b"x-trace-id"]) == 32
    assert packets[1]["body"] == b"event: done\n\n"


def make_result(value, contract="0x" + "2" * 40, at="2026-10-10T10:00:00Z"):
    chain = {
        "chain_id": 80094,
        "chain_name": "Berachain",
        "computed_at": at,
        "status": "complete",
        "stale": False,
        "assets": [{"symbol": "BERA", "contract": contract, "amount": "1", "value_usd": value}],
    }
    return {
        "id": value,
        "currency": "USD",
        "chains": [chain],
        "isComplete": True,
        "failed_chains": [],
        "queriedAt": at,
        "scope": "registered_tokens",
        "unpriced": [],
    }


async def test_aggregate_uses_latest_asset_once_and_preserves_failure():
    old = make_result("100")
    fresh = make_result("125", at="2026-10-10T11:00:00Z")
    fresh["isComplete"] = False
    fresh["failed_chains"] = [1]
    service = ResultService(None, None)
    service.get = AsyncMock(side_effect=[old, fresh])
    service.save = AsyncMock(side_effect=lambda user, payload: payload)
    result = await service.aggregate("u", ["a", "b"])
    assert result["total_value"] == "125"
    assert not result["isComplete"] and result["failed_chains"] == [1]
    assert len(result["sources"]) == 2


def test_metadata_projection_cannot_copy_amounts():
    payload = make_result("123456.789")
    safe = json.dumps(summary(payload))
    assert "123456.789" not in safe.replace('"result_id": "123456.789"', "")
    assert "amount" not in safe and "address" not in safe and "value_usd" not in safe


class QueryModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "controlled-query"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if messages[-1].type == "tool":
            message = AIMessage(content="查询完成，请查看数据卡片。")
        else:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_my_token_balance",
                        "args": {"symbol": "BERA", "chain_id": 80094},
                        "id": "call_test",
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


async def test_real_langchain_tool_loop_only_sees_metadata():
    client = SimpleNamespace(
        request=AsyncMock(
            return_value={"result_id": "r1", "isComplete": True, "symbols": ["BERA"], "unpriced_count": 0}
        ),
        default_chain_id=80094,
    )
    repository = SimpleNamespace(add_ref=AsyncMock())
    context = RunContext("u", "c", "private-token", client, repository)
    agent = create_agent(QueryModel(), tools=TOOLS, context_schema=RunContext, checkpointer=InMemorySaver())
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "查询BERA余额"}]},
        context=context,
        config={"configurable": {"thread_id": "c"}},
    )
    client.request.assert_awaited_once()
    assert context.results[0]["result_id"] == "r1"
    serialized = json.dumps([m.model_dump(mode="json") for m in result["messages"]], ensure_ascii=False)
    assert "private-token" not in serialized and "value_usd" not in serialized
