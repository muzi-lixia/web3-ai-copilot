"""工具公共基座：跨文件复用的参数类。

参数类默认跟自己的工具放在同一个文件里，只有被两个以上工具复用的才提到这里。
"""

from pydantic import BaseModel, ConfigDict, Field


class Arguments(BaseModel):
    """全部工具参数的共同基类。

    只声明业务字段；runtime 由框架在执行阶段注入，因此这里不能禁止未声明字段。
    """

    # 忽略框架注入的字段（当前版本会把 runtime 一并交给 Pydantic 校验）。
    model_config = ConfigDict(extra="ignore")


class ChainArguments(Arguments):
    """只含可选网络的参数：省略 chain_id 表示不限定网络。"""

    chain_id: int | None = Field(default=None, gt=0, description="链 ID，省略查询已支持网络")


class PricedArguments(ChainArguments):
    """在网络之上追加计价货币。

    currency 用 pattern 收窄成两种取值，避免模型传入后端无法换算的货币代码。
    """

    currency: str = Field(default="USD", pattern="^(USD|CNY)$", description="估值计价货币")
