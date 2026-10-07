"""跨模块共享的类型与基类。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

# 以太坊地址：0x + 40 位十六进制。
# 这里只做格式校验；EIP-55 checksum 归一化在 services 层用 web3 完成（schemas 不依赖 web3）。
Address = Annotated[str, StringConstraints(pattern=r"^0x[a-fA-F0-9]{40}$")]


class Schema(BaseModel):
    """所有契约模型的基类。

    统一 `extra="ignore"`：上游数据源多返回字段时不应炸掉整个响应；
    缺字段由各模型自身的必填/可选声明负责。
    """

    model_config = ConfigDict(extra="ignore")


class ErrorBody(Schema):
    code: str
    message: str


class ErrorResponse(Schema):
    """与 core/errors.py 抛出的响应体结构一致，仅供 OpenAPI 文档展示。"""

    error: ErrorBody
