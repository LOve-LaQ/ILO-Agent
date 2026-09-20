# Security - 密码哈希与 JWT
"""
阶段 2 权限边界的密码学工具。

- 密码哈希：passlib + bcrypt（bcrypt 钉在 4.0.1，passlib 1.7.4 读取
  `bcrypt.__about__` 依赖它；>=4.1 会直接 AttributeError）
- Token：PyJWT HS256，用 `typ` 声明区分 access / refresh / email_verify，
  避免把长效 refresh token 当 access token 使用（邮箱确认链接同理，不能当登录凭证）
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional, Tuple
import uuid

import jwt
from passlib.context import CryptContext

from src.core.config import settings

TokenType = Literal["access", "refresh", "email_verify"]

# PyJWT 对 HS256 有最短密钥长度告警（RFC 7518 建议 >= 哈希输出长度）
MIN_SECRET_BYTES = 32

# bcrypt 只使用前 72 字节，注册时限制密码长度避免「超长密码静默等价」
MAX_PASSWORD_BYTES = 72

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(raw: str) -> str:
    """生成密码哈希"""
    return _pwd_context.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    """校验密码；库中哈希格式异常时按「不匹配」处理，不向上抛 500"""
    try:
        return _pwd_context.verify(raw, hashed)
    except ValueError:
        return False


def _require_secret() -> str:
    secret = settings.jwt_secret
    if not secret:
        raise RuntimeError(
            "未配置 JWT_SECRET，无法签发或校验 token。"
            "请在 backend/.env 中设置（见 backend/.env.example）。"
        )
    length = len(secret.encode("utf-8"))
    if length < MIN_SECRET_BYTES:
        raise RuntimeError(
            f"JWT_SECRET 至少需要 {MIN_SECRET_BYTES} 字节，当前仅 {length} 字节。"
        )
    return secret


def create_token(
    subject: str,
    token_type: TokenType = "access",
    *,
    jti: Optional[str] = None,
    sid: Optional[str] = None,
) -> Tuple[str, datetime]:
    """签发 token，返回 (token, 过期时间)。

    - `jti`：token 唯一 id。refresh token 的 jti 会落进 `user_sessions`，
      用于「按 token 撤销会话」与 reuse 检测（阶段 5 会话可撤销）。
    - `sid`：所属会话 id。access token 也带它，这样撤销某会话时把 sid 写进
      黑名单就能让该会话的 access **立即**失效，而不必等它 30 分钟自然过期。
    """
    now = datetime.now(timezone.utc)
    if token_type == "access":
        expires_at = now + timedelta(minutes=settings.access_token_expire_minutes)
    elif token_type == "refresh":
        expires_at = now + timedelta(days=settings.refresh_token_expire_days)
    else:
        # email_verify：只放进邮件链接。不轮换、不落库，靠 `typ` 与登录凭证互不通用；
        # 确认邮箱是幂等状态（点多次结果一致），因此不需要「用过即废」的一次性表。
        expires_at = now + timedelta(hours=settings.email_verification_expire_hours)

    payload: dict[str, Any] = {
        "sub": subject,
        "typ": token_type,
        "jti": jti or uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if sid:
        payload["sid"] = sid
    token = jwt.encode(payload, _require_secret(), algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_token(token: str, expected_type: TokenType = "access") -> dict:
    """解码并校验 token；失败时抛出 jwt.PyJWTError 子类，由调用方翻译成业务错误"""
    payload = jwt.decode(token, _require_secret(), algorithms=[settings.jwt_algorithm])
    if payload.get("typ") != expected_type:
        raise jwt.InvalidTokenError(f"token 类型不匹配（期望 {expected_type}）")
    if not payload.get("sub"):
        raise jwt.InvalidTokenError("token 缺少 sub 声明")
    return payload


def access_token_expires_in() -> int:
    """access token 有效期（秒），返回给前端用于提前刷新"""
    return settings.access_token_expire_minutes * 60
