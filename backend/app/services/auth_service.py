"""钱包登录：nonce 签发 → 验签 → JWT 签发 / 解析。

三个设计决定：

1. **签名原文由后端下发。** 前端拿到的 `message` 原样签回，不在前端拼模板。
   两端各拼一次必然在空格、换行或地址大小写上产生差异，症状是"签名明明签了却验不过"。
   模板只有一个出处，这类问题就不存在。

2. **nonce 一次性。** 验签前先 pop 删除，无论成功失败都不会留在池子里。
   同一个签名提交第二次必然失败 —— 这是防重放的最省事做法。

3. **地址以签名反推为准。** 不用请求体里带的 address 下结论：签名能还原出地址，
   说明对方确实持有私钥；请求体里的 address 只是用来指定"要验哪个 nonce"。
"""

import secrets
import time
from datetime import UTC, datetime, timedelta
from threading import RLock

import jwt
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address

from app.config.settings import settings
from app.shared.errors import NonceInvalidError, RateLimitError, SignatureInvalidError, UnauthorizedError

# 签名原文模板。用英文是刻意的：钱包（尤其硬件钱包）对非 ASCII 消息的展示与编码支持不一，
# 中文消息在部分钱包里会显示成乱码，用户根本不知道自己在签什么。
_MESSAGE_TEMPLATE = """{domain} wants you to sign in with your Ethereum account:
{address}

Sign this message to verify wallet ownership.
This will not trigger a blockchain transaction or cost any gas.

Nonce: {nonce}
"""

# 进程内 nonce 池：address(lower) -> (nonce, message, 过期时间戳)
#
# ⚠️ V1 单进程够用。多进程 / 多实例部署时必须换 Redis：
# 否则请求落到另一个进程就查不到 nonce，表现为"随机登录失败"。
_nonces: dict[str, tuple[str, str, float]] = {}
_state_lock = RLock()
_rates: dict[str, tuple[float, int]] = {}


def cleanup_auth_state() -> None:
    """Called periodically by lifespan and before writes; storage remains bounded."""
    now = time.time()
    tick = time.monotonic()
    with _state_lock:
        for key, (_, _, expires) in list(_nonces.items()):
            if expires <= now:
                del _nonces[key]
        for key, (expires, _) in list(_rates.items()):
            if expires <= tick:
                del _rates[key]


def check_auth_rate(peer: str) -> None:
    """Single-process, per transport peer limit. Never trust arbitrary forwarded headers."""
    cleanup_auth_state()
    now = time.monotonic()
    with _state_lock:
        expires, count = _rates.get(peer, (now + 60, 0))
        if peer not in _rates and len(_rates) >= settings.auth_rate_max_entries:
            raise RateLimitError("登录请求过多，请稍后重试")
        if count >= settings.auth_rate_per_minute:
            raise RateLimitError("登录请求过于频繁，请稍后重试")
        _rates[peer] = (expires, count + 1)


def _now() -> datetime:
    return datetime.now(UTC)


def issue_nonce(address: str) -> tuple[str, str, datetime]:
    """为该地址生成新 nonce，覆盖旧的（同一地址重复请求只有最后一个有效）。"""
    checksum = to_checksum_address(address)
    nonce = secrets.token_hex(16)
    expires_at = _now() + timedelta(seconds=settings.nonce_ttl_seconds)
    message = _MESSAGE_TEMPLATE.format(domain=settings.siwe_domain, address=checksum, nonce=nonce)
    cleanup_auth_state()
    with _state_lock:
        if address.lower() not in _nonces and len(_nonces) >= settings.nonce_max_entries:
            raise RateLimitError("登录请求已达容量上限，请稍后重试")
        _nonces[address.lower()] = (nonce, message, expires_at.timestamp())
    return nonce, message, expires_at


def verify_login(address: str, signature: str) -> str:
    """校验签名，返回 checksum 格式的地址。失败抛 AppError。

    先消耗 nonce 再验签：即使验签失败，这个 nonce 也已作废，无法被反复试探。
    """
    with _state_lock:
        entry = _nonces.pop(address.lower(), None)
    if entry is None:
        raise NonceInvalidError("请先获取 nonce，或 nonce 已过期作废")

    _nonce, message, expires_ts = entry
    if time.time() >= expires_ts:
        raise NonceInvalidError("nonce 已过期，请重新发起登录")

    try:
        recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception as exc:  # noqa: BLE001  —— 签名格式非法种类多，统一归为验签失败
        raise SignatureInvalidError("签名无法解析") from exc

    if recovered.lower() != address.lower():
        raise SignatureInvalidError("签名地址与请求地址不一致")

    return to_checksum_address(recovered)


def create_token(address: str) -> tuple[str, datetime]:
    """签发 JWT。"""
    expires_at = _now() + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {
        "sub": address.lower(),
        "iat": int(time.time()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_token(token: str) -> str:
    """解析 JWT 并返回地址（小写）。失败抛 Unauthorized 类异常。"""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "iat", "exp"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("登录已过期，请重新连接钱包") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("登录凭证无效") from exc

    address = payload.get("sub")
    if not address:
        raise UnauthorizedError("登录凭证缺少地址")
    try:
        return to_checksum_address(str(address))
    except Exception as exc:  # noqa: BLE001
        raise UnauthorizedError("登录凭证中的地址不合法") from exc
