"""LangSmith 默认仅记录运行信息；可显式启用脱敏内容以调试模型与工具。"""

import re

from langsmith import Client, configure


def safe_metadata(metadata):
    """只允许服务端生成的关联标识，过滤 SDK 自动附加的其他元数据。"""
    return {
        key: value for key, value in metadata.items()
        if key in {"trace_id", "turn_id", "session_id", "run_id"}
        and isinstance(value, str) and re.fullmatch(r"[a-f0-9-]{16,36}", value)
    }


def anonymize(payload):
    """error 独立于 inputs/outputs，明确替换；其余内容默认拒绝外发。"""
    return {**safe_metadata(payload), **({"error": "redacted"} if "error" in payload else {})}


def configure_tracing(settings):
    if not settings.langsmith_api_key.get_secret_value():
        configure(client=None, enabled=False)
        return None
    capture = settings.langsmith_capture_content
    scrub = content_anonymizer([
        getattr(settings, key).get_secret_value()
        for key in ("langsmith_api_key", "deepseek_api_key", "qwen_api_key")
    ]) if capture else anonymize
    client = Client(
        api_key=settings.langsmith_api_key.get_secret_value(), timeout_ms=5000,
        hide_inputs=not capture, hide_outputs=not capture, hide_metadata=safe_metadata, anonymizer=scrub,
    )
    configure(client=client, enabled=True, project_name=settings.langsmith_project)
    return client


def content_anonymizer(secrets=()):
    """调试模式保留消息与工具结果，递归遮蔽可识别凭证及运行上下文。"""
    protected = tuple(sorted(
        (value for value in secrets if value and len(value) >= 4), key=len, reverse=True
    ))
    token_metrics = {"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
                     "total_tokens", "max_tokens", "max_output_tokens"}
    sensitive = re.compile(
        r"api.?key|secret|password|authorization|cookie|signature|private.?key|mnemonic|seed.?phrase"
        r"|credential|access.?token|refresh.?token", re.I
    )

    def clean(value):
        if isinstance(value, dict):
            return {
                key: "[redacted]" if (
                    str(key).lower() in {"token", "runtime", "context", "client", "repository", "user_id"}
                    or (str(key).lower() not in token_metrics and sensitive.search(str(key)))
                ) else clean(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        if isinstance(value, str):
            for secret in protected:
                value = value.replace(secret, "[redacted]")
            value = re.sub(r"Bearer\s+[^\s\"'<>]+", "Bearer [redacted]", value, flags=re.I)
            value = re.sub(r"sk-(?:proj-|ant-)?[A-Za-z0-9_-]{12,}", "[redacted]", value)
            value = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[redacted]", value)
            value = re.sub(r"0x[a-fA-F0-9]{40,}(?![a-fA-F0-9])", "[redacted]", value)
            value = re.sub(r"(?<![a-fA-F0-9])[a-fA-F0-9]{64}(?![a-fA-F0-9])", "[redacted]", value)
            value = re.sub(r"(\w+://)[^\s/:@]+:[^\s/@]+@", r"\1[redacted]@", value)
            value = re.sub(
                r"(?i)((?:api[_-]?key|password|secret|refresh[_-]?token|access[_-]?token)"
                r"[\"']?\s*[:=]\s*[\"']?)[^\s,;\"'}]+", r"\1[redacted]", value,
            )
            return value
        if value is None or isinstance(value, (bool, int, float)):
            return value
        # 不调用任意对象的 repr（可能包含客户端、上下文和凭证）。
        return "[redacted]"

    def scrub(payload):
        # SDK 同一个 anonymizer 也处理 metadata；保持关联元数据白名单。
        if any(key in payload for key in ("trace_id", "turn_id", "session_id", "run_id", "ls_provider")):
            if not any(key in payload for key in ("messages", "input", "output", "query", "error")):
                return safe_metadata(payload)
        return clean(payload)

    return scrub
