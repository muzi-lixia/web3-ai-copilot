"""Agent 的唯一业务访问通道：HTTP 转发用户凭证和追踪标识，不解析钱包地址。"""

import httpx

from app.core.exceptions import AppError, UnauthorizedError, UpstreamError
from app.core.logging import span, span_id, trace_id


class FoundationClient:
    def __init__(self, settings, *, transport=None):
        self.http = httpx.AsyncClient(
            base_url=settings.foundation_url + settings.api_prefix,
            timeout=settings.tool_timeout_seconds,
            trust_env=False,
            transport=transport,
        )

    async def request(self, method, path, token, **kwargs):
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
        return await self.request("GET", "/auth/identity", token)

    async def close(self):
        await self.http.aclose()
