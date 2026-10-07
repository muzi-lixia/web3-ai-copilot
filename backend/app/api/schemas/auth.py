"""钱包登录契约：nonce → 签名 → JWT。"""

from datetime import datetime

from app.api.schemas.common import Address, Schema


class NonceRequest(Schema):
    address: Address


class NonceResponse(Schema):
    address: str
    nonce: str
    message: str
    """前端要签的**完整原文**。

    由后端拼好下发，前端原样签回 —— 两端各拼一次模板必然会有空格/换行差异，
    表现为验签莫名其妙失败，且极难排查。模板只有一个出处就不会有这个问题。
    """
    expires_at: datetime


class VerifyRequest(Schema):
    address: Address
    signature: str
    """钱包签名结果，0x + 130 位十六进制（r/s/v）。"""


class TokenResponse(Schema):
    token: str
    address: str
    expires_at: datetime


class MeResponse(Schema):
    address: str
