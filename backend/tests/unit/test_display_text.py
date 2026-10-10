"""用户原文只进入展示字段，安全模型输入与重试链路独立。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent.api import TurnRequest, submit
from app.agent.privacy import normalize_query


def request(services):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=services)))


async def test_submit_preserves_original_input_but_model_uses_safe_text():
    raw = "  查询我的BERA余额，我有123456.789个BERA\n"
    chat = SimpleNamespace(submit=AsyncMock(return_value={"turn_id": "turn"}))
    body = TurnRequest(message=raw, client_msg_id=uuid4())
    await submit(body, request(SimpleNamespace(chat=chat)), {"user_id": "user", "token": "token"})
    call = chat.submit.await_args
    assert call.kwargs["display_text"] == raw
    assert call.args[3].text == normalize_query(raw).text
    assert "123456.789" not in call.args[3].text


async def test_retry_reuses_original_display_and_safe_model_input():
    raw = "  查一下我的BERA\n"
    safe = normalize_query(raw).text
    previous = {"query": safe, "display_text": raw}
    repository = SimpleNamespace(read=AsyncMock(return_value=previous))
    chat = SimpleNamespace(submit=AsyncMock(return_value={"turn_id": "retry"}))
    original = uuid4()
    await submit(
        TurnRequest(retry_of=original, client_msg_id=uuid4()),
        request(SimpleNamespace(chat=chat, repository=repository)),
        {"user_id": "user", "token": "token"},
    )
    repository.read.assert_awaited_once_with("user", str(original))
    assert chat.submit.await_args.kwargs["display_text"] == raw
    assert chat.submit.await_args.args[3].text == safe


def test_blank_input_rejected_without_trimming_valid_messages():
    with pytest.raises(ValidationError):
        TurnRequest(message=" \n ", client_msg_id=uuid4())
    assert TurnRequest(message=" 查BERA \n", client_msg_id=uuid4()).message == " 查BERA \n"


@pytest.mark.parametrize(
    "raw",
    [
        "你好",
        "你好，请问你能做什么？",
        "我有两个问题",
        "我有1个问题：BERA是什么？",
        "再看一下它",
        "在80094查询BERA余额",
        "能解释一下余额查询吗？",
    ],
)
def test_natural_language_is_not_rewritten_or_blocked(raw):
    safe = normalize_query(raw)
    assert safe.text == raw
    assert safe.clarification is None


def test_redaction_preserves_question_and_only_allows_validated_contracts():
    address = "0x" + "1" * 40
    raw = f"你好，我有10个BERA，请帮我查询合约{address}的余额"
    safe = normalize_query(raw)
    assert safe.text == "你好，我有[金额已隐藏]个BERA，请帮我查询合约[地址已隐藏]的余额"
    assert address in normalize_query(raw, public_contracts=(address,)).text
    assert "10" not in normalize_query(raw, public_contracts=(address,)).text
