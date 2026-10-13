"""受理环节的凭证交接回归：一轮执行只用受理时换来的委托凭证。

用户令牌只需在受理那一刻有效 —— 换证时校验一次，之后过期不影响这一轮。
这里锁住三件事：换证发生在建轮次之前、后台生成拿到的是委托凭证而不是用户令牌、
以及委托凭证的寿命必须覆盖一整轮（配短了服务应该起不来）。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from pydantic import ValidationError

from app.agent.service import ChatService
from app.core.config import Settings
from app.core.exceptions import UnauthorizedError
from app.foundation.auth import AuthService, Identity


@pytest.fixture
def settings():
    """不读 .env 的配置：本用例只关心委托凭证的时间预算。"""
    return Settings(_env_file=None)


@pytest.fixture
def chat(settings):
    repository = SimpleNamespace(
        conversation=AsyncMock(return_value={"id": "c1"}),
        find_client_turn=AsyncMock(return_value=None),
        create_turn=AsyncMock(return_value=("t1", False)),
    )
    client = SimpleNamespace(request=AsyncMock(return_value={"permit": "p1"}))
    lease = SimpleNamespace(verify=AsyncMock())
    return ChatService(repository, client, settings, lease, None)


def test_permit_must_outlive_the_longest_turn():
    """委托凭证寿命短于一轮的最长耗时是配置错误：服务应该起不来，而不是每问必错。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_permit_seconds=60)


def test_permit_carries_its_scope_and_session(settings):
    """委托凭证带 agent 用途与所属会话：鉴权照常过会话检查，登出即失效。"""
    service = AuthService(SimpleNamespace(), settings)
    identity = Identity("u1", "s1", "0xAbC0000000000000000000000000000000000001")

    payload = jwt.decode(
        service.issue_agent_permit(identity), settings.jwt_secret, algorithms=["HS256"]
    )

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
