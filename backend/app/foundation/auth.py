"""持久化 SIWE 登录。事务完成 nonce 消费与刷新轮换；不在进程内保存身份状态。"""

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address
from sqlalchemy import text

from app.core.exceptions import NonceInvalidError, RateLimitError, UnauthorizedError
from app.core.logging import span, trace_id


@dataclass(frozen=True)
class Identity:
    user_id: str
    session_id: str
    address: str = field(repr=False)


class AuthService:
    """基础服务唯一身份权威。Agent 通过 HTTP 取得无地址的身份表示。"""

    def __init__(self, database, settings):
        self.db, self.settings = database, settings

    async def audit(self, event, status, user_id=None):
        """审计单独持久化，只有身份 ID、事件及结果；不保存签名、token 或原文。"""
        async with self.db.sessions.begin() as s:
            await s.execute(
                text("""INSERT INTO security_audit(user_id,event,status,trace_id)
                VALUES(:u,:e,:s,:t)"""),
                dict(u=user_id, e=event, s=status, t=trace_id.get()),
            )

    async def limit(self, key, maximum):
        """数据库原子计数；不同 worker 不会各自放行一份配额。key 不落原始 IP 或钱包。"""
        bucket = hashlib.sha256(key.encode()).hexdigest()
        window = int(time.time()) // 60
        async with self.db.sessions.begin() as s:
            count = await s.scalar(
                text("""INSERT INTO request_limits(bucket,bucket_window,count)
                VALUES(:b,:w,1) ON CONFLICT(bucket,bucket_window)
                DO UPDATE SET count=request_limits.count+1 RETURNING count"""),
                dict(b=bucket, w=window),
            )
        if count > maximum:
            raise RateLimitError("请求过于频繁，请稍后重试")

    async def challenge(self, address):
        """构造固定域名与 URI 的 SIWE 消息；客户端只能签署完整服务端消息。"""
        address = to_checksum_address(address)
        nonce = secrets.token_hex(16)
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=self.settings.nonce_ttl_seconds)
        message = (
            f"{self.settings.siwe_domain} wants you to sign in with your Ethereum account:\n"
            f"{address}\n\nSign in to Web3 Copilot. No transaction will be sent.\n\n"
            f"URI: {self.settings.siwe_uri}\nVersion: 1\n"
            f"Chain ID: {self.settings.default_chain_id}\nNonce: {nonce}\n"
            f"Issued At: {now.isoformat()}\nExpiration Time: {expires.isoformat()}"
        )
        async with self.db.sessions.begin() as s:
            await s.execute(
                text("""INSERT INTO auth_challenges(nonce,address,message,expires_at)
                VALUES(:n,:a,:m,:e) ON CONFLICT(address) DO UPDATE SET
                nonce=excluded.nonce,message=excluded.message,expires_at=excluded.expires_at"""),
                dict(n=nonce, a=address.lower(), m=message, e=expires),
            )
        return dict(address=address, nonce=nonce, message=message, expires_at=expires)

    async def login(self, message, signature):
        """只支持 EOA；从服务端已下发原文匹配挑战，验签通过后同事务消费。

        两个并发签名请求锁定同一挑战行，只有一个能创建会话。绝不接受模型身份。
        合约钱包 ERC-1271 需要专门验签适配，当前明确不隐式支持。
        """
        with span("auth.login"):
            async with self.db.sessions.begin() as s:
                row = (
                    (
                        await s.execute(
                            text("""SELECT * FROM auth_challenges
                    WHERE message=:m AND expires_at>now() FOR UPDATE"""),
                            dict(m=message),
                        )
                    )
                    .mappings()
                    .first()
                )
                try:
                    if row is None:
                        raise ValueError("missing challenge")
                    recovered = Account.recover_message(
                        encode_defunct(text=row["message"]), signature=signature
                    )
                    if recovered.lower() != row["address"]:
                        raise ValueError("signature mismatch")
                except Exception:
                    raise NonceInvalidError("签名挑战无效，请重新获取并签名") from None
                await s.execute(text("DELETE FROM auth_challenges WHERE nonce=:n"), dict(n=row["nonce"]))
                user = await s.scalar(
                    text("""INSERT INTO service_users(id,address) VALUES(:id,:a)
                    ON CONFLICT(address) DO UPDATE SET address=excluded.address RETURNING id"""),
                    dict(id=str(uuid4()), a=row["address"]),
                )
                sid, refresh = str(uuid4()), secrets.token_urlsafe(48)
                expires = datetime.now(UTC) + timedelta(days=self.settings.login_session_days)
                await s.execute(
                    text("INSERT INTO login_sessions(id,user_id,expires_at) VALUES(:s,:u,:e)"),
                    dict(s=sid, u=user, e=expires),
                )
                await s.execute(
                    text("INSERT INTO refresh_credentials(digest,session_id) VALUES(:d,:s)"),
                    dict(d=self.digest(refresh), s=sid),
                )
            await self.audit("login", "success", user)
            return self.tokens(user, sid, row["address"], refresh, expires)

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def tokens(self, user, sid, address, refresh, session_expires):
        """JWT 的 exp 不越过登录会话绝对期限；不会将刷新 token 放入 JWT。"""
        expires = min(
            datetime.now(UTC) + timedelta(minutes=self.settings.access_token_minutes), session_expires
        )
        token = jwt.encode(
            dict(sub=user, sid=sid, address=address, iat=int(time.time()), exp=int(expires.timestamp())),
            self.settings.jwt_secret,
            algorithm="HS256",
        )
        return dict(
            token=token,
            token_type="Bearer",
            address=to_checksum_address(address),
            user_id=user,
            refresh_token=refresh,
            expires_at=expires,
            session_expires_at=session_expires,
        )

    async def authenticate(self, token):
        """签名与会话状态缺一不可；撤销后即使 JWT 尚未到期也不能访问业务。"""
        try:
            payload = jwt.decode(
                token,
                self.settings.jwt_secret,
                algorithms=["HS256"],
                options={"require": ["sub", "sid", "address", "iat", "exp"]},
            )
        except jwt.ExpiredSignatureError:
            raise UnauthorizedError("访问凭证已过期，请续期", code="access_expired") from None
        except jwt.InvalidTokenError:
            raise UnauthorizedError("登录凭证无效") from None
        async with self.db.sessions() as s:
            row = (
                await s.execute(
                    text("""SELECT u.address FROM login_sessions l
                JOIN service_users u ON u.id=l.user_id WHERE l.id=:sid AND l.user_id=:u
                AND NOT l.revoked AND l.expires_at>now()"""),
                    dict(sid=payload["sid"], u=payload["sub"]),
                )
            ).first()
        if row is None or row.address != payload["address"]:
            raise UnauthorizedError("登录会话已失效，请重新登录")
        return Identity(payload["sub"], payload["sid"], to_checksum_address(row.address))

    async def refresh(self, token):
        """原子轮换，旧 token 重用会撤销整次登录。撤销必须提交后才能抛错。"""
        denied = True
        result = None
        async with self.db.sessions.begin() as s:
            row = (
                (
                    await s.execute(
                        text("""SELECT r.*,l.user_id,l.expires_at,l.revoked,u.address
                FROM refresh_credentials r JOIN login_sessions l ON l.id=r.session_id
                JOIN service_users u ON u.id=l.user_id WHERE r.digest=:d FOR UPDATE OF r,l"""),
                        dict(d=self.digest(token)),
                    )
                )
                .mappings()
                .first()
            )
            if row and row["consumed"]:
                await s.execute(
                    text("UPDATE login_sessions SET revoked=true WHERE id=:s"), dict(s=row["session_id"])
                )
            elif row and not row["revoked"] and row["expires_at"] > datetime.now(UTC):
                fresh = secrets.token_urlsafe(48)
                await s.execute(
                    text("UPDATE refresh_credentials SET consumed=true WHERE digest=:d"),
                    dict(d=self.digest(token)),
                )
                await s.execute(
                    text("INSERT INTO refresh_credentials(digest,session_id) VALUES(:d,:s)"),
                    dict(d=self.digest(fresh), s=row["session_id"]),
                )
                result = self.tokens(
                    row["user_id"], row["session_id"], row["address"], fresh, row["expires_at"]
                )
                denied = False
        await self.audit("refresh", "failed" if denied else "success", row["user_id"] if row else None)
        if denied:
            raise UnauthorizedError("续期凭证无效，请重新登录")
        return result

    async def logout(self, identity):
        async with self.db.sessions.begin() as s:
            await s.execute(
                text("UPDATE login_sessions SET revoked=true WHERE id=:s"), dict(s=identity.session_id)
            )
        await self.audit("logout", "success", identity.user_id)

    async def cleanup(self):
        """小规模维护采用数据库批量删除；无需 Redis TTL 或独立任务队列。"""
        async with self.db.sessions.begin() as s:
            await s.execute(text("DELETE FROM auth_challenges WHERE expires_at<now()"))
            await s.execute(text("SELECT cleanup_asset_results()"))
            await s.execute(
                text("DELETE FROM request_limits WHERE bucket_window<:w"), dict(w=int(time.time()) // 60 - 2)
            )
            await s.execute(
                text(
                    "DELETE FROM refresh_credentials WHERE session_id IN "
                    "(SELECT id FROM login_sessions WHERE expires_at<now() OR revoked)"
                )
            )
