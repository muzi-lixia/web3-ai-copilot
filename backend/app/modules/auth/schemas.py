"""钱包登录契约：nonce → 签名 → JWT。"""

from datetime import datetime

from pydantic import ConfigDict

from app.common.schemas import Address, Schema


class NonceRequest(Schema):
    """申请挑战的地址输入，只做格式校验，此阶段尚未证明钱包所有权。"""

    model_config = ConfigDict(extra="forbid")
    address: Address


class NonceResponse(Schema):
    """一次性挑战与签名原文；客户端完整签署 message，不自行重新拼接模板。"""

    address: str
    nonce: str
    message: str
    """前端要签的**完整原文**。

    由后端拼好下发，前端原样签回 —— 两端各拼一次模板必然会有空格/换行差异，
    表现为验签莫名其妙失败，且极难排查。模板只有一个出处就不会有这个问题。
    """
    expires_at: datetime


class VerifyRequest(Schema):
    """提交钱包地址和签名；地址用于找到挑战，最终身份仍由签名恢复结果决定。"""

    model_config = ConfigDict(extra="forbid")
    address: Address
    # 服务端使用自己的挑战原文恢复签名，客户端不能替换 message。
    signature: str
    """钱包签名结果，0x + 130 位十六进制（r/s/v）。"""


class TokenResponse(Schema):
    """登录成功后的 JWT、已确认的钱包地址和失效时间；后续请求使用 Bearer 头。"""

    token: str
    token_type: str = "Bearer"
    address: str
    expires_at: datetime


class MeResponse(Schema):
    """当前有效登录凭据对应的钱包身份，供客户端恢复登录状态。"""

    address: str
