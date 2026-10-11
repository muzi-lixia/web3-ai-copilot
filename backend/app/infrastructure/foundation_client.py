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

    async def request(self, method, path, token, **kwargs):
        """带凭证调用基础服务，并把上游错误统一翻译成本服务错误。

        401 区分「登录整体失效」与「凭证过期」两种码；其余错误不回传上游原文，
        避免通过错误内容泄露真实数据。204 返回 None，否则取 data 字段。
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
            if response.status_code == 401:
                code = "access_expired" if response.json().get("code") == 40103 else "unauthorized"
                raise UnauthorizedError("登录凭证已失效", code=code)
            if response.is_error:
                # 不转发上游原文、请求 URL 或签名，避免通过错误内容泄露真实数据。
                error = AppError("业务查询失败，请检查参数或稍后重试", code="business_query_failed")
                error.status_code = response.status_code
                raise error
            return None if response.status_code == 204 else response.json()["data"]

    async def identity(self, token):
        """用凭证换当前用户身份，是 Agent 侧唯一的身份来源。"""
        return await self.request("GET", "/auth/identity", token)

    async def close(self):
        """释放连接池，在应用停机时调用。"""
        await self.http.aclose()
