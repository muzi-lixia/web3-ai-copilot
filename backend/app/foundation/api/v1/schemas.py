"""基础 API 请求体公共约束。"""

from pydantic import BaseModel, ConfigDict


class Input(BaseModel):
    """请求体基类：统一禁止多余字段，避免前端多传的字段被静默忽略。"""

    model_config = ConfigDict(extra="forbid")
