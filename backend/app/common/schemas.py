"""跨模块共享的类型与基类。"""

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# 以太坊地址：0x + 40 位十六进制。
# 这里只做格式校验；EIP-55 checksum 归一化在基础设施的链客户端层用 web3 完成（schemas 不依赖 web3）。
Address = Annotated[str, StringConstraints(pattern=r"^0x[a-fA-F0-9]{40}$")]


class Schema(BaseModel):
    """所有契约模型的基类。

    统一 `extra="ignore"`：上游数据源多返回字段时不应炸掉整个响应；
    缺字段由各模型自身的必填/可选声明负责。
    """

    model_config = ConfigDict(extra="ignore")


class DataIssue(Schema):
    """一项可展示的数据缺失原因。

    code 用于程序判断，message 用于说明，asset 可定位受影响资产；缺值不能伪装成零。
    """

    code: str
    message: str
    asset: str | None = None


class DataQuality(Schema):
    """统一表达数据完整度及原因的响应基类。

    complete 表示当前计算所需数据齐全；partial 表示部分可用；unavailable 表示
    无法给出可靠结果。具体业务必须同时提供 issues，便于调用方理解缺失原因。
    """

    status: Literal["complete", "partial", "unavailable"] = "complete"
    issues: list[DataIssue] = Field(default_factory=list)


T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """项目统一成功响应：code=0、msg=success，data 承载真实业务数据。

    HTTP 状态码独立表达创建、接受和失败语义。204 无正文，SSE 不套 JSON 包装。
    """

    code: Literal[0] = 0
    msg: str = "success"
    data: T
