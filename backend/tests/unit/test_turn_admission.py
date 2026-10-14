"""受理环节的凭证交接回归：一轮执行只用受理时换来的委托凭证。

用户令牌只需在受理那一刻有效 —— 换证时校验一次，之后过期不影响这一轮。
这里锁住三件事：换证发生在建轮次之前、后台生成拿到的是委托凭证而不是用户令牌、
以及委托凭证的寿命必须覆盖一整轮（不足时拒绝受理且不创建轮次）。
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest

from app.agent.config import AgentSettings
from app.agent.service import ChatService
from app.core.exceptions import ChatUnavailableError, UnauthorizedError
from app.foundation.config import FoundationSettings
from app.modules.identity.service import AuthService, Identity


@pytest.fixture
def settings(tmp_path):
    """不读 .env 的配置：本用例只关心委托凭证的时间预算。"""
    return AgentSettings(_env_file=None, agent_outbox_dir=str(tmp_path / "outbox"))


@pytest.fixture
def chat(settings):
    repository = SimpleNamespace(
        conversation=AsyncMock(return_value={"id": "c1"}),
        find_client_turn=AsyncMock(return_value=None),
        create_turn=AsyncMock(return_value=("t1", False)),
    )
    client = SimpleNamespace(
        request=AsyncMock(return_value={"permit": "p1", "expires_at": time.time() + 300})
    )
    lease = SimpleNamespace(verify=AsyncMock())
    return ChatService(repository, client, settings, lease, None)


@pytest.mark.parametrize("expires_at", [None, True, "300", 0, float("nan"), float("inf")])
async def test_invalid_permit_expiry_does_not_create_turn(chat, expires_at):
    chat.client.request.return_value = {"permit": "p1", "expires_at": expires_at}
    with pytest.raises(ChatUnavailableError):
        await chat.submit("u1", "token", "client-id", SimpleNamespace(text="你好"))
    chat.repository.create_turn.assert_not_awaited()


async def test_short_permit_does_not_create_turn(chat):
    chat.client.request.return_value = {"permit": "p1", "expires_at": time.time() + 60}
    with pytest.raises(ChatUnavailableError):
        await chat.submit("u1", "token", "client-id", SimpleNamespace(text="你好"))
    chat.repository.create_turn.assert_not_awaited()


def test_permit_carries_its_scope_and_session():
    """委托凭证带 agent 用途与所属会话：鉴权照常过会话检查，登出即失效。"""
    settings = FoundationSettings(_env_file=None)
    service = AuthService(SimpleNamespace(), settings)
    identity = Identity("u1", "s1", "0xAbC0000000000000000000000000000000000001")

    payload = jwt.decode(service.issue_agent_permit(identity), settings.jwt_secret, algorithms=["HS256"])

    assert payload["scope"] == "agent"
    assert (payload["sub"], payload["sid"]) == ("u1", "s1")
    assert payload["exp"] - payload["iat"] == settings.agent_permit_seconds


async def test_generation_runs_on_the_permit_not_the_user_token(chat):
    chat.repository.create_turn.return_value = ("t1", True)
    chat.generate = AsyncMock()
    safe = SimpleNamespace(text="你好", clarification=None)

    await chat.submit("u1", "user-token", "client-id", safe)
    await asyncio.sleep(0)

    chat.client.request.assert_awaited_once_with("POST", "/auth/agent-permits", "user-token")
    assert chat.generate.call_args.args[3] == "p1"


async def test_minting_failure_happens_before_any_row_is_created(chat):
    """换证失败就不该建轮次：建了再失败等于让用户白等一轮。"""
    chat.client.request.side_effect = UnauthorizedError("访问凭证已过期", code="access_expired")
    safe = SimpleNamespace(text="你好", clarification=None)

    with pytest.raises(UnauthorizedError):
        await chat.submit("u1", "user-token", "client-id", safe)

    assert not chat.repository.create_turn.called


async def test_terminal_turn_persists_queue_and_execution_timings(chat, settings):
    from app.agent.privacy import SafeInput

    chat.settings = settings
    chat.repository.model_provider = AsyncMock(return_value="ollama")
    chat.repository.update = AsyncMock()
    chat.client.identity = AsyncMock()
    # 用明确追问的确定性路径检查受理、额度与终态记录，不依赖模型生成。
    await chat.generate("u", "c", "t", "permit", SafeInput(text="q", clarification="请补充网络"))
    final = chat.repository.update.await_args.kwargs
    assert final["state"] == "completed"
    timings = final["timings"]
    assert timings["queue_ms"] >= 0
    assert timings["execution_ms"] >= 0
    assert timings["total_ms"] >= timings["queue_ms"]
    assert timings["first_token_ms"] is None


async def test_stream_records_time_to_first_model_text(chat, settings, monkeypatch):
    from contextlib import asynccontextmanager

    from langchain_core.messages import AIMessageChunk

    from app.agent.privacy import SafeInput

    @asynccontextmanager
    async def models(*args):
        yield object(), object(), settings

    class Agent:
        async def astream(self, *args, **kwargs):
            yield AIMessageChunk(content="回答", id="m"), {"langgraph_node": "model"}

    monkeypatch.setattr("app.agent.execution.chat_models", models)
    monkeypatch.setattr("app.agent.execution.create_agent", lambda *args, **kwargs: Agent())
    chat.repository.model_provider = AsyncMock(return_value="ollama")
    chat.repository.update = AsyncMock()
    chat.client.identity = AsyncMock()
    await chat.generate("u", "c", "t", "permit", SafeInput(text="q"))
    final = chat.repository.update.await_args.kwargs
    assert final["state"] == "completed"
    assert final["answer"] == "回答"
    assert final["timings"]["first_token_ms"] is not None
    assert final["timings"]["first_token_ms"] >= final["timings"]["queue_ms"]
