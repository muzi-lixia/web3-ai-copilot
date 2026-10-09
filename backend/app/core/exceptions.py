"""跨层业务异常。HTTP 响应映射位于 core.exception_handlers。"""


class AppError(Exception):
    """业务异常基类。"""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        """保存可公开的错误说明，允许调用方细分错误码；默认 HTTP 状态由异常类型决定。"""
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class InvalidAddressError(AppError):
    """地址格式非法（非 0x + 40 hex / checksum 校验失败）。"""

    status_code = 400
    code = "invalid_address"


class UpstreamError(AppError):
    """外部依赖失败：RPC 全部节点不可用 / 行情源全部不可用且无缓存可降级。"""

    status_code = 502
    code = "upstream_error"


class NotFoundError(AppError):
    """资源不存在或不属于当前用户；统一 404，避免暴露他人资源的存在。"""

    status_code = 404
    code = "not_found"


class ForbiddenError(AppError):
    """已登录，但访问的资源不属于当前身份。"""

    status_code = 403
    code = "forbidden"


class UnsupportedChainError(AppError):
    """请求的 chain_id 不在链注册表里。"""

    status_code = 400
    code = "unsupported_chain"


class UnauthorizedError(AppError):
    """未登录，或 token 缺失 / 过期 / 非法。"""

    status_code = 401
    code = "unauthorized"


class NonceInvalidError(AppError):
    """nonce 不存在或已过期（超时，或同一个 nonce 被重复提交）。"""

    status_code = 400
    code = "nonce_invalid"


class SignatureInvalidError(AppError):
    """签名无法解析，或反推出的地址与请求地址不一致。"""

    status_code = 401
    code = "signature_invalid"


class RateLimitError(AppError):
    """登录频率或鉴权状态容量达到限制，调用方应稍后再试。"""

    status_code = 429
    code = "rate_limited"


class ChatUnavailableError(AppError):
    """运行时未启动或单实例租约丢失，拒绝继续调度和写入。"""

    status_code = 503
    code = "chat_unavailable"


class ConflictError(AppError):
    """聊天状态冲突：已有活动生成、幂等 ID 被挪用，或重试条件不满足。"""

    status_code = 409
    code = "chat_busy"


class ContextTooLargeError(AppError):
    """模型输入超过安全预算，明确拒绝请求以避免上游静默截断。"""

    status_code = 422
    code = "model_context_too_large"
