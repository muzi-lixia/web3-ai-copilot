"""Agent 的唯一业务访问通道：HTTP 转发用户凭证和追踪标识，不解析钱包地址。"""

import httpx

from app.core.exceptions import AppError, UnauthorizedError, UpstreamError
from app.core.logging import span, span_id, trace_id


class FoundationClient:
    """对基础服务的 HTTP 客户端，只转发凭证与追踪头，不接触用户地址。"""

    def __init__(self, settings, *, transport=None):
        """建长连接客户端；trust_env=False 保证外呼不依赖环境变量代理，transport 供测试注入。"""
        self.http = httpx.AsyncClient(
            base_url=settings.foundation_url + settings.api_prefix,
            timeout=settings.tool_timeout_seconds,
            trust_env=False,
            transport=transport,
        )

    @staticmethod
    def _error_body(response):
        """尽力取出基础服务的错误 body；不是 JSON 就返回空字典。

        基础服务的 msg 与数字业务码是设计上就公开的文案（连 500 都被兜底成
        「服务内部错误」，见 core.http.errors），可以原样带给调用方；
        这里只防它返回非 JSON body。
        """
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    async def request(self, method, path, token, **kwargs):
        """带凭证调用基础服务，并把上游错误翻译成本服务错误。

        401 区分「登录整体失效」与「凭证过期」两种码；其余错误保留基础服务自己的
        公开说明（msg + 数字业务码），不带请求 URL 或签名。204 返回 None，否则取 data。
        """
        with span("foundation.call"):
            try:
                response = await self.http.request(
                    method,
                    path,
                    headers={
                        "Authorization": "Bearer " + token,
                        "X-Trace-Id": trace_id.get(),
                        "X-Parent-Span-Id": span_id.get(),
                    },
                    **kwargs,
                )
            except httpx.HTTPError:
                raise UpstreamError("基础服务暂不可用", code="foundation_unavailable") from None
            body = self._error_body(response) if response.is_error else {}
            if response.status_code == 401:
                code = "access_expired" if body.get("code") == 40103 else "unauthorized"
                raise UnauthorizedError("登录凭证已失效", code=code)
            if response.is_error:
                # 原文不再丢弃：只剩一句通用文案时，调用方无从判断是缺参数还是上游故障。
                error = AppError(
                    body.get("msg") or "业务查询失败，请稍后重试", code="business_query_failed"
                )
                error.status_code = response.status_code
                error.upstream_code = body.get("code")
                raise error
            return None if response.status_code == 204 else response.json()["data"]

    async def identity(self, token):
        """用凭证换当前用户身份，是 Agent 侧唯一的身份来源。"""
        return await self.request("GET", "/auth/identity", token)

    async def close(self):
        """释放连接池，在应用停机时调用。"""
        await self.http.aclose()
