"""无需外网的模型协议回归：模拟 Ollama 输出，验证上下文透传、终态和错误语义。"""

import json

import httpx
import pytest

from app.ai.conversation.prompts import SYSTEM_PROMPT
from app.ai.llm.ollama import OllamaClient
from app.ai.memory.service import estimate_tokens, validate_summary
from app.core.config import settings
from app.core.exceptions import UpstreamError

ollama = OllamaClient(settings)


def model_stub(monkeypatch, handler):
    """用 httpx 的模拟传输替换网络层，隔离本机代理和 Ollama 运行状态。"""
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


def stream_body(*events):
    """编码成 Ollama 原生的逐行 JSON，验证协议层而不是直接伪造业务事件。"""
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events).encode()


async def test_ollama_stream_forwards_context_and_usage(monkeypatch):
    """验证角色顺序和原文完整透传，并检查正文增量拼接及模型用量记录。"""
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=stream_body(
                {"message": {"content": "你好，"}, "done": False},
                {"message": {"content": "小林"}, "done": True, "prompt_eval_count": 42, "eval_count": 5},
            ),
        )

    model_stub(monkeypatch, handler)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "我叫小林"},
        {"role": "assistant", "content": "你好"},
        {"role": "user", "content": "我叫什么？"},
    ]
    events = [event async for event in ollama.stream_chat(messages)]
    assert "".join(event["text"] for event in events if event["type"] == "delta") == "你好，小林"
    assert events[-1]["usage"] == {"prompt_tokens": 42, "completion_tokens": 5}
    assert calls[0]["stream"] is True
    assert calls[0]["messages"] == messages


@pytest.mark.parametrize(
    "status,body,code",
    [
        (404, {"error": "private details"}, "model_unavailable"),
        (200, {"message": {"content": ""}, "done": True}, "model_empty_response"),
        (200, [], "model_unavailable"),
    ],
)
async def test_model_errors_are_actionable(monkeypatch, status, body, code):
    """模型缺失、空回答和非法载荷必须转换成明确业务错误，不能泄漏上游私有错误内容。"""
    model_stub(monkeypatch, lambda request: httpx.Response(status, content=stream_body(body)))
    with pytest.raises(UpstreamError) as error:
        _ = [event async for event in ollama.stream_chat([{"role": "user", "content": "你好"}])]
    assert error.value.code == code
    assert "private details" not in str(error.value)


async def test_model_timeout(monkeypatch):
    """读取超时必须表现为模型超时，而不是普通成功或无说明的空回复。"""

    def handler(request):
        raise httpx.ReadTimeout("timeout", request=request)

    model_stub(monkeypatch, handler)
    with pytest.raises(UpstreamError, match="超时"):
        _ = [event async for event in ollama.stream_chat([])]


async def test_truncated_upstream_stream_is_not_success(monkeypatch):
    """已经收到文本但没有 done 的断流必须失败，保留半段正文但不标记完整。"""
    model_stub(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            content=stream_body(
                {"message": {"content": "半段回复"}, "done": False},
            ),
        ),
    )
    source = ollama.stream_chat([])
    assert (await anext(source))["text"] == "半段回复"
    with pytest.raises(UpstreamError, match="提前结束"):
        await anext(source)


def test_summary_requires_real_source_ids_and_bounded_sections():
    """摘要出处必须属于已处理原文；同时检查中文 token 估算和输入预算预留。"""
    value = {key: [] for key in ("facts", "constraints", "decisions", "open_questions")}
    value["facts"] = [{"text": "用户叫小林", "sources": ["message-1"]}]
    assert validate_summary(value, {"message-1"}) == value
    with pytest.raises(ValueError, match="source"):
        validate_summary(value, {"other-message"})
    assert estimate_tokens("中文") == 2
    assert settings.copilot_input_budget < 16384


async def test_unsafe_context_rejected_before_model_request(monkeypatch):
    """长中文输入不能仅凭粗估值放行；超限必须在发送 HTTP 前明确拒绝。"""
    from app.core.exceptions import ContextTooLargeError

    def unexpected(request):
        pytest.fail("超限输入不应请求模型")

    model_stub(monkeypatch, unexpected)
    with pytest.raises(ContextTooLargeError):
        _ = [event async for event in ollama.stream_chat([{"role": "user", "content": "中" * 10000}])]
    body = ollama.request_body([{"role": "user", "content": "你好"}])
    assert body["options"]["num_ctx"] == settings.copilot_context_window
    assert body["options"]["num_predict"] == settings.copilot_output_tokens
