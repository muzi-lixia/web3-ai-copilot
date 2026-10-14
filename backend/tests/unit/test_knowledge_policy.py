"""使用真实 Agent/工具节点复现：不调用模型也必须检索，检索故障不能生成假无答案。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.config import AgentSettings
from app.agent.context import RunContext
from app.agent.knowledge_policy import RequireKnowledge, needs_retrieval
from app.agent.middleware import ContextBudget, LiveDataFilter
from app.agent.tools.knowledge import search_docs
from app.core.exceptions import KnowledgeRetrievalError
from app.infrastructure.foundation_client import FoundationClient


class NeverChoosesTools(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self):
        return "controlled-no-tool-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="知识库没有找到相关依据。"))])


def make_agent(client, settings):
    context = RunContext("u", "c", "private-permit", client, SimpleNamespace())
    model = NeverChoosesTools()
    graph = create_agent(model, tools=[search_docs], context_schema=RunContext,
                         checkpointer=InMemorySaver(),
                         middleware=[RequireKnowledge(), LiveDataFilter(), ContextBudget(settings)])
    return graph, context, model


@pytest.mark.parametrize("status,body,expected", [
    (409, {"code": 40904, "msg": "知识库向量版本不兼容", "data": None}, "knowledge_index_incompatible"),
    (502, {"code": 50201, "msg": "embedding 服务不可用", "data": None}, "knowledge_retrieval_failed"),
])
async def test_knowledge_failure_prevents_model_from_claiming_no_answer(status, body, expected):
    settings = AgentSettings(_env_file=None)
    client = FoundationClient(settings, transport=httpx.MockTransport(lambda request: httpx.Response(
        status, json=body,
    )))
    graph, context, model = make_agent(client, settings)
    try:
        with pytest.raises(KnowledgeRetrievalError) as exc:
            await graph.ainvoke({"messages": [HumanMessage(content="berachain是什么？")]},
                                context=context, config={"configurable": {"thread_id": "c"}})
        assert exc.value.code == expected
        assert model.calls == 0
        assert context.tool_calls[0]["tool"] == "search_docs"
        assert context.tool_calls[0]["origin"] == "policy"
        assert context.tool_calls[0]["status"] == "failed"
        assert context.tool_calls[0]["error_code"] == expected
        assert context.tool_calls[0]["upstream_code"] == body["code"]
        assert context.results == []
    finally:
        await client.close()


async def test_same_question_searches_again_each_turn_even_after_no_answer():
    settings = AgentSettings(_env_file=None)
    client = SimpleNamespace(request=AsyncMock(return_value={"query": "berachain是什么？", "hits": []}))
    graph, context, model = make_agent(client, settings)
    config = {"configurable": {"thread_id": "c"}}
    for _ in range(2):
        await graph.ainvoke({"messages": [HumanMessage(content="berachain是什么？")]},
                            context=context, config=config)
    assert client.request.await_count == 2
    assert model.calls == 2
    assert all(call["status"] == "ok" for call in context.tool_calls)
    assert all(result["body"]["hits"] == [] for result in context.results)


@pytest.mark.parametrize("query,expected", [
    ("berachain是什么？", True), ("Berachain", True), ("它的机制呢？", True),
    ("What is Berachain?", True), ("介绍价格形成机制", True),
    ("你好", False), ("谢谢！", False), ("我的BERA余额", False), ("BERA多少钱", False),
    ("支持哪些链", False),
])
def test_retrieval_policy_keeps_data_queries_and_greetings_on_existing_route(query, expected):
    assert needs_retrieval(query) is expected


@pytest.mark.parametrize("with_tool", [True, False])
def test_old_knowledge_claims_do_not_enter_current_model_context(with_tool):
    history = [HumanMessage(content="berachain是什么？")]
    if with_tool:
        history.extend([
            AIMessage(content="", tool_calls=[
                {"id": "old", "name": "search_docs", "args": {"query": "berachain"}}
            ]),
            ToolMessage(content="索引版本不兼容", tool_call_id="old", status="error"),
        ])
    history.extend([AIMessage(content="知识库没有关于 berachain 的信息"), HumanMessage(content="它是什么？")])
    filtered = LiveDataFilter()._filtered(history)
    text = "\n".join(message.text for message in filtered)
    assert "知识库没有关于" not in text
    assert "索引版本不兼容" not in text
    assert filtered[-1].text == "它是什么？"
    assert filtered[0].text == "berachain是什么？"


async def test_index_failure_is_persisted_as_failed_turn_without_a_fabricated_answer(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    from app.agent.privacy import SafeInput
    from app.agent.service import ChatService

    settings = AgentSettings(_env_file=None, agent_outbox_dir=str(tmp_path))
    model = NeverChoosesTools()

    @asynccontextmanager
    async def models(*args):
        yield model, model, settings

    monkeypatch.setattr("app.agent.execution.chat_models", models)

    def respond(request):
        if request.url.path.endswith("/identity"):
            return httpx.Response(200, json={"code": 0, "data": {"user_id": "u"}})
        return httpx.Response(409, json={"code": 40904, "msg": "知识库向量版本不兼容", "data": None})

    client = FoundationClient(settings, transport=httpx.MockTransport(respond))
    repository = SimpleNamespace(model_provider=AsyncMock(return_value="ollama"), update=AsyncMock())
    chat = ChatService(repository, client, settings, SimpleNamespace(verify=AsyncMock()), InMemorySaver())
    try:
        await chat.generate("u", "c", "t", "permit", SafeInput(text="berachain是什么？"))
        saved = repository.update.await_args.kwargs
        assert saved["state"] == "failed"
        assert saved["error"] == "knowledge_index_incompatible"
        assert saved["answer"] == ""
        assert saved["tool_trace"][0]["status"] == "failed"
        assert saved["tool_trace"][0]["upstream_code"] == 40904
        assert model.calls == 0
    finally:
        await client.close()


async def test_long_knowledge_question_fails_without_a_tool_retry_loop():
    from app.core.exceptions import BadRequestError

    settings = AgentSettings(_env_file=None)
    client = SimpleNamespace(request=AsyncMock())
    graph, context, model = make_agent(client, settings)
    with pytest.raises(BadRequestError) as exc:
        await graph.ainvoke({"messages": [HumanMessage(content="讲解" + "a" * 1000)]},
                            context=context, config={"configurable": {"thread_id": "c"}})
    assert exc.value.code == "knowledge_query_too_long"
    assert client.request.await_count == 0 and model.calls == 0


async def test_retry_after_index_failure_does_not_reuse_orphan_tool_call_or_old_conclusion():
    settings = AgentSettings(_env_file=None)
    responses = [
        httpx.Response(409, json={"code": 40904, "msg": "索引不兼容", "data": None}),
        httpx.Response(200, json={"code": 0, "data": {"query": "berachain是什么？", "hits": []}}),
    ]
    client = FoundationClient(settings, transport=httpx.MockTransport(lambda request: responses.pop(0)))
    graph, context, model = make_agent(client, settings)
    config = {"configurable": {"thread_id": "c"}}
    try:
        with pytest.raises(KnowledgeRetrievalError):
            await graph.ainvoke({"messages": [HumanMessage(content="berachain是什么？")]},
                                context=context, config=config)
        await graph.ainvoke({"messages": [HumanMessage(content="berachain是什么？")]},
                            context=context, config=config)
        assert len(context.tool_calls) == 2
        assert [call["status"] for call in context.tool_calls] == ["failed", "ok"]
        assert model.calls == 1
    finally:
        await client.close()
