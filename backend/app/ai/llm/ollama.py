"""项目唯一模型适配器：Ollama 原生请求、输入预算、流解析和摘要生成。

配置统一取 Settings；不负责用户鉴权、历史持久化或摘要内容的语义校验。
"""

import json
from collections.abc import AsyncIterator

import httpx

from app.core.config import Settings
from app.core.exceptions import ContextTooLargeError, UpstreamError


def prompt_upper_bound(messages: list[dict]) -> int:
    """对当前 Qwen 字节级分词使用 UTF-8 字节数作为偏保守上界，再保留每条消息的模板开销。

    这是长度防线，不是精确 tokenizer；切换到其他模型时仍需验证分词与模板。
    不能把 estimate_tokens 的经验值当成防止截断的硬保证。
    """
    return sum(len(message["content"].encode("utf-8")) + 64 for message in messages)


class OllamaClient:
    """实现 ChatModel 协议；新增提供方只需替换容器中的模型适配器。"""

    def __init__(self, settings: Settings):
        """持有当前应用的模型配置；不在构造时连接 Ollama 或发起推理。"""
        self.settings = settings

    def validate_context(self, messages: list[dict], *, summary: bool = False) -> None:
        """复用硬预算判断，超限时主动拒绝，保护系统提示与当前问题不被静默截断。"""
        if not self.fits_context(messages, summary):
            raise ContextTooLargeError("模型上下文超过安全预算，请缩短当前问题")

    def fits_context(self, messages: list[dict], summary=False) -> bool:
        """将 UTF-8 长度上界、当前任务输出预算和安全余量一起与窗口比较。

        summary=True 使用摘要输出上限。该检查偏保守，未使用模型的精确 tokenizer。
        """
        output = (
            self.settings.copilot_summary_output_tokens if summary else self.settings.copilot_output_tokens
        )
        return (
            prompt_upper_bound(messages) + output + self.settings.copilot_context_reserve
            <= self.settings.copilot_context_window
        )

    def request_body(self, messages: list[dict], *, summary=False) -> dict:
        """统一构造回答和摘要请求，发出前先校验输入长度。

        回答采用流式低温度输出；摘要采用更低温度、非流式和 JSON 格式。
        num_ctx 与 num_predict 来自配置，不能与记忆策略各自维护一套硬编码预算。
        """
        self.validate_context(messages, summary=summary)
        options = {
            "temperature": 0 if summary else 0.3,
            "num_ctx": self.settings.copilot_context_window,
            "num_predict": self.settings.copilot_summary_output_tokens
            if summary
            else self.settings.copilot_output_tokens,
        }
        body = {
            "model": self.settings.copilot_model,
            "messages": messages,
            "stream": not summary,
            "options": options,
        }
        # JSON 输出模式只约束摘要的编码；业务字段及原文出处仍要由记忆层校验。
        if summary:
            body["format"] = "json"
        return body

    def transport(
        self,
    ):
        """本机请求不走环境代理；连接和读取的超时策略由同一份配置控制。"""
        return httpx.AsyncClient(trust_env=False, timeout=self.settings.copilot_timeout_seconds)

    def chat_url(
        self,
    ):
        """拼接 Ollama 原生聊天地址，去除配置末尾斜杠，避免重复路径分隔符。"""
        return f"{self.settings.copilot_ollama_url.rstrip('/')}/api/chat"

    async def stream_chat(self, messages: list[dict]) -> AsyncIterator[dict]:
        """读取 Ollama 的逐行 JSON，输出 delta 和 done；缺少 done 的断流必须作为失败，不能误标完成。"""
        body = self.request_body(messages)
        answer = ""
        try:
            # 本机模型请求不走 HTTP_PROXY/HTTPS_PROXY，避免环境代理阻断 localhost。
            async with self.transport() as client:
                async with client.stream(
                    "POST",
                    self.chat_url(),
                    json=body,
                ) as response:
                    response.raise_for_status()
                    # Ollama 输出 NDJSON，并非 SSE；一行 JSON 可以跨多个 HTTP 数据块，由 httpx 拼接。
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        body = json.loads(line)
                        if not isinstance(body, dict) or body.get("error"):
                            raise UpstreamError("Ollama 流式回复异常，请重试", code="model_unavailable")
                        message = body.get("message", {})
                        text = message.get("content", "") if isinstance(message, dict) else ""
                        if not isinstance(text, str):
                            raise ValueError("Invalid model content")
                        if text:
                            answer += text
                            yield {"type": "delta", "text": text}
                        # done 才是成功终止标志；流读取结束本身不能说明回复完整。
                        if body.get("done") is True:
                            if not answer.strip():
                                raise UpstreamError("模型未返回有效回答，请重试", code="model_empty_response")
                            yield {
                                "type": "done",
                                "model": self.settings.copilot_model,
                                "partial": body.get("done_reason") == "length",
                                # 记录模型的实际 token 用量，供上下文估算与性能评估对照。
                                "usage": {
                                    "prompt_tokens": body.get("prompt_eval_count", 0),
                                    "completion_tokens": body.get("eval_count", 0),
                                },
                            }
                            return
                    raise UpstreamError("模型连接提前结束，请重试", code="model_stream_interrupted")
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise UpstreamError("模型回复超时，请稍后重试", code="model_timeout") from exc
        except httpx.HTTPStatusError as exc:
            message = (
                f"Ollama 未找到模型 {self.settings.copilot_model}，请先下载该模型"
                if exc.response.status_code == 404
                else "Ollama 模型服务暂时不可用，请稍后重试"
            )
            raise UpstreamError(message, code="model_unavailable") from exc
        except (httpx.RequestError, ValueError) as exc:
            raise UpstreamError(
                "无法连接或读取 Ollama，请确认模型服务已启动", code="model_unavailable"
            ) from exc

    async def summarize(self, messages: list[dict]) -> dict:
        """使用低温度和 JSON 格式生成完整摘要；字段、出处与长度由 memory 层进一步校验。"""
        try:
            # 本机模型请求不走 HTTP_PROXY/HTTPS_PROXY，避免环境代理阻断 localhost。
            async with self.transport() as client:
                response = await client.post(
                    self.chat_url(),
                    json=self.request_body(messages, summary=True),
                )
                response.raise_for_status()
                return json.loads(response.json()["message"]["content"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise UpstreamError("摘要生成失败", code="summary_failed") from exc
