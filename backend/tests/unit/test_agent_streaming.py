"""拆分后 SSE 与终态恢复状态的交互，以及订阅资源释放。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.streaming import TurnStream


def make_stream(pending):
    row = {"turn_id": "t", "version": 1, "status": "completed",
           "message": {"content": "answer", "results": []}}
    repository = SimpleNamespace(read=AsyncMock(return_value=row), snapshot=lambda value: dict(value))
    return TurnStream(repository, SimpleNamespace(identity=AsyncMock()),
                      SimpleNamespace(verify=AsyncMock()), SimpleNamespace(pending=pending))


async def test_terminal_stream_releases_subscription():
    stream = make_stream({})
    frames = [frame async for frame in stream.events("u", "t", "token")]
    assert [frame["type"] for frame in frames] == ["done"]
    assert frames[0]["message"]["content"] == "answer"
    assert stream.live == {}
    stream.client.identity.assert_awaited_once_with("token")


async def test_persistence_recovery_wakes_stream_and_finishes_it():
    pending = {"t": ("u", {})}
    stream = make_stream(pending)
    source = stream.events("u", "t", "token")
    first = await anext(source)
    assert first["type"] == "text_delta" and first["persistence_pending"]
    pending.clear()
    stream.broadcast("t", None)
    final = await anext(source)
    assert final["type"] == "done" and not final["persistence_pending"]
    with pytest.raises(StopAsyncIteration):
        await anext(source)
    assert stream.live == {}
