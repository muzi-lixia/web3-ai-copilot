"""工具公共基座：跨文件复用的参数类，以及走 ResultService 的查询出口。

参数类默认跟自己的工具放在同一个文件里，只有被两个以上工具复用的才提到这里。
"""

import json

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.logging import span


class Arguments(BaseModel):
    """全部工具参数的共同基类。

    只声明业务字段；runtime 由框架在执行阶段注入，因此这里不能禁止未声明字段。
    """

    # 忽略框架注入的字段（当前版本会把 runtime 一并交给 Pydantic 校验）。
    model_config = ConfigDict(extra="ignore")


class ChainArguments(Arguments):
    """只含可选网络的参数：省略 chain_id 表示不限定网络。"""

    chain_id: int | None = Field(default=None, gt=0, description="链 ID，省略查询已支持网络")


class OverviewArguments(ChainArguments):
    """概览类查询参数：网络可选，另加展示货币。

    currency 用 pattern 收窄成两种取值，避免模型传入后端无法换算的货币代码。
    """

    currency: str = Field(default="USD", pattern="^(USD|CNY)$", description="估值计价货币")


class BalanceArguments(OverviewArguments):
    """指定币种的查询参数：在概览参数上追加一个必填符号。"""

    symbol: str = Field(
        min_length=1, max_length=64, description="精确币种符号，例如 BERA 或 WBERA；二者不可混用"
    )

    @model_validator(mode="after")
    def contract_requires_chain(self):
        """代币合约脱离网络无法解析，必须在参数阶段拒绝，而不是留给下游报错。"""
        if self.symbol.startswith("0x") and self.chain_id is None:
            raise ValueError("代币合约必须同时指定网络")
        return self


async def query(runtime, name, payload):
    """查询类工具的唯一出口：转发基础服务、登记结果引用，只把摘要交给模型。

    Args:
        runtime: 带可信身份的框架上下文；身份不来自模型参数。
        name: 工具名，仅用于埋点。
        payload: 发送给基础服务的业务参数。
    Returns:
        基础服务返回的摘要 JSON（含金额与余额，不含地址与合约）；
        完整结果进展示通道，随快照下发给前端。
    """
    # 身份与凭证只从 run context 取，不经过任何模型参数。
    context = runtime.context
    with span("tool.completed", tool=name):
        # 基础服务一次返回两份投影：summary（给模型）与 result（给前端）。
        meta = await context.client.request("POST", "/me/balance-results", context.token, json=payload)
        result_id = meta["result"]["result_id"]
        await context.repository.add_ref(context.user_id, context.conversation_id, result_id)
        context.results.append(meta["result"])
        # 模型只收到白名单摘要；业务数据走 results，不进模型消息。
        return json.dumps(meta["summary"], ensure_ascii=False)
