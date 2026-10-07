"""Agent 接口的请求 / 响应契约。"""

from pydantic import Field

from app.schemas.common import Address, Schema
from app.schemas.risk import RiskReport


class ChatRequest(Schema):
    message: str = Field(min_length=1, max_length=2000)
    address: Address | None = Field(default=None, description="涉及链上数据的意图需要；纯知识问答可省略")
    conversation_id: str | None = None


class AnalyzePortfolioRequest(Schema):
    address: Address


class AnalyzePortfolioResponse(Schema):
    """确定性路径：指标全部算完才返回，`explanation` 可能为 None（LLM 失败不影响数据可用）。"""

    address: str
    report: RiskReport
