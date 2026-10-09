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

from app.core.config import Settings
from app.core.exceptions import NonceInvalidError, RateLimitError, SignatureInvalidError, UnauthorizedError

# 签名原文模板。用英文是刻意的：钱包（尤其硬件钱包）对非 ASCII 消息的展示与编码支持不一，
# 中文消息在部分钱包里会显示成乱码，用户根本不知道自己在签什么。
_MESSAGE_TEMPLATE = """{domain} wants you to sign in with your Ethereum account:
{address}

Sign this message to verify wallet ownership.
This will not trigger a blockchain transaction or cost any gas.

Nonce: {nonce}
"""


class AuthService:
    """单实例鉴权服务；状态属于应用实例，替换 nonce 存储无需改 HTTP 路由。"""

    def __init__(self, settings: Settings) -> None:
        """建立当前应用独立的挑战池和来源限流池。

        nonce 只在当前进程有效，重启后用户须重新发起挑战；已签发 JWT 不依赖
        挑战池。RLock 保护同步函数可能由不同线程访问时的读取、删除与计数。
        """
        self.settings = settings
        self._nonces: dict[str, tuple[str, str, float]] = {}
        self._rates: dict[str, tuple[float, int]] = {}
        self._state_lock = RLock()

    def cleanup_auth_state(
        self,
    ) -> None:
        """回收过期挑战与来源限流窗口，使两个进程内字典保持有界。

        lifespan 定时调用，新增状态前也调用；先复制条目再删除，避免遍历时修改
        字典。nonce 采用实际失效时间，限流采用不受系统校时影响的单调时钟。
        """
        now = time.time()
        tick = time.monotonic()
        with self._state_lock:
            for key, (_, _, expires) in list(self._nonces.items()):
                if expires <= now:
                    del self._nonces[key]
            for key, (expires, _) in list(self._rates.items()):
                if expires <= tick:
                    del self._rates[key]

    # 校验鉴权频率，防暴力请求
    def check_auth_rate(self, peer: str) -> None:
        """按实际连接来源限制一分钟内的登录请求数，nonce 与验签共享计数。

        单进程状态由锁保护，容量满时拒绝新增来源；已有来源仍按计数校验。
        不信任任意转发头，该实现也不提供跨 worker 的共享限流。
        """
        self.cleanup_auth_state()
        now = time.monotonic()
        with self._state_lock:
            expires, count = self._rates.get(peer, (now + 60, 0))
            if peer not in self._rates and len(self._rates) >= self.settings.auth_rate_max_entries:
                raise RateLimitError("登录请求过多，请稍后重试")
            if count >= self.settings.auth_rate_per_minute:
                raise RateLimitError("登录请求过于频繁，请稍后重试")
            self._rates[peer] = (expires, count + 1)

    def _now(
        self,
    ) -> datetime:
        """返回带 UTC 时区的当前时间，用于生成明确的 nonce 和 JWT 失效时刻。"""
        return datetime.now(UTC)  # 使用UTC时间，避免时区问题

    def issue_nonce(self, address: str) -> tuple[str, str, datetime]:
        """为该地址生成新 nonce，覆盖旧的（同一地址重复请求只有最后一个有效）。"""
        checksum = to_checksum_address(address)
        nonce = secrets.token_hex(16)  # 生成16字节随机hex字符串，作为一次性nonce
        expires_at = self._now() + timedelta(seconds=self.settings.nonce_ttl_seconds)
        message = _MESSAGE_TEMPLATE.format(domain=self.settings.siwe_domain, address=checksum, nonce=nonce)
        self.cleanup_auth_state()
        with self._state_lock:  # 加锁，保证字典操作线程安全
            # 判断：地址不在池子里，并且当前nonce总数已经达到最大容量
            if address.lower() not in self._nonces and len(self._nonces) >= self.settings.nonce_max_entries:
                raise RateLimitError("登录请求已达容量上限，请稍后重试")
            # 存入字典：覆盖该地址旧的nonce
            self._nonces[address.lower()] = (nonce, message, expires_at.timestamp())
        return nonce, message, expires_at

    def read_challenge(self, nonce: str) -> dict:
        """按随机挑战 ID 读取未过期资源，不消费挑战或清理状态。"""
        from app.core.exceptions import NotFoundError

        with self._state_lock:
            for address, (value, message, expires) in self._nonces.items():
                if secrets.compare_digest(value, nonce) and time.time() < expires:
                    return {
                        "address": address,
                        "nonce": value,
                        "message": message,
                        "expires_at": datetime.fromtimestamp(expires, UTC),
                    }
        raise NotFoundError("挑战不存在或已失效")

    def verify_login(self, address: str, signature: str) -> str:
        """校验签名，返回 checksum 格式的地址。失败抛 AppError。

        先消耗 nonce 再验签：即使验签失败，这个 nonce 也已作废，无法被反复试探。
        """
        with self._state_lock:
            entry = self._nonces.pop(address.lower(), None)
        if entry is None:
            raise NonceInvalidError("请先获取 nonce，或 nonce 已过期作废")

        _nonce, message, expires_ts = entry
        if time.time() >= expires_ts:
            raise NonceInvalidError("nonce 已过期，请重新发起登录")

        try:
            # EIP-191 personal_sign 消息恢复，只证明消息签名，不触发链上交易。
            recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
        except Exception as exc:  # noqa: BLE001  —— 签名格式非法种类多，统一归为验签失败
            raise SignatureInvalidError("签名无法解析") from exc

        if recovered.lower() != address.lower():
            raise SignatureInvalidError("签名地址与请求地址不一致")

        return to_checksum_address(recovered)

    def create_token(self, address: str) -> tuple[str, datetime]:
        """为验签成功的地址签发 JWT，返回 token 与 UTC 失效时刻。

        sub 只保存钱包身份，iat 和 exp 分别记录签发和到期时间；不把 nonce
        放进 token，因此后续请求无需再读取挑战池。此方法的调用方须先完成验签。
        """
        expires_at = self._now() + timedelta(minutes=self.settings.jwt_expire_minutes)
        payload = {
            "sub": address.lower(),
            "iat": int(time.time()),
            "exp": int(expires_at.timestamp()),
        }
        token = jwt.encode(payload, self.settings.jwt_secret, algorithm=self.settings.jwt_algorithm)
        return token, expires_at

    def decode_token(self, token: str) -> str:
        """验证 JWT 的签名、算法及必填时间字段，返回 EIP-55 checksum 钱包地址。

        签发时 sub 使用小写以统一身份，但返回值经过 checksum 归一化；数据库
        和归属比较仍会转小写。只允许配置的算法，拒绝缺少 sub、iat、exp 的凭据。
        """
        try:
            payload = jwt.decode(
                token,
                self.settings.jwt_secret,
                algorithms=[self.settings.jwt_algorithm],
                options={"require": ["sub", "iat", "exp"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise UnauthorizedError("登录已过期，请重新连接钱包") from exc
        except jwt.InvalidTokenError as exc:
            raise UnauthorizedError("登录凭证无效") from exc

        # 地址还需格式校验，防止合法签名但载荷身份格式无效的 JWT 进入业务层。
        address = payload.get("sub")
        if not address:
            raise UnauthorizedError("登录凭证缺少地址")
        try:
            return to_checksum_address(str(address))
        except Exception as exc:  # noqa: BLE001
            raise UnauthorizedError("登录凭证中的地址不合法") from exc
