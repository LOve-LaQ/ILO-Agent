# Password Reset - 找回密码令牌
"""阶段 5 找回密码：一次性重置令牌的签发与核验。

**只存哈希**：明文 token 只在「生成 → 放进邮件链接」这一瞬间存在，库里落的是
sha256。库被拖走也换不出可用链接（见 models/password_reset.py）。

**为什么要「防邮件轰炸」**：攻击者若拿到某人的邮箱，反复触发找回会把这台用户
的收件箱刷爆，也浪费 SMTP 配额。约定：同一用户 60 秒内已有未用且未过期的令牌时，
直接**复用它、不再发新邮件** —— 由于我们只存哈希、无法回读明文，正确做法是
「跳过本次发信」而不是「换发一枚新令牌」。
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from src.core.config import settings
from src.models.password_reset import PasswordResetToken
from src.models.user import User

# token_urlsafe(32) ≈ 43 字符、256 bit 熵：足够不可枚举
TOKEN_BYTES = 32

# 同用户在该窗口内重复申请时不再发新邮件（防轰炸）
REUSE_WINDOW_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _unused_tokens(db: OrmSession, user_id) -> list[PasswordResetToken]:
    now = _now()
    return list(
        db.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > now,
            )
        )
        .scalars()
        .all()
    )


def issue_token(db: OrmSession, user: User) -> Optional[str]:
    """为一个用户签发重置令牌。

    返回明文令牌（供邮件链接使用）；若命中了「60 秒防轰炸窗口」则返回 None，
    表示「已有一枚有效令牌，本次跳过发信」。
    """
    now = _now()
    existing = _unused_tokens(db, user.id)

    if existing:
        newest = max(existing, key=lambda row: row.created_at)
        age = (now - newest.created_at).total_seconds()
        if age <= REUSE_WINDOW_SECONDS:
            return None

    # 换发新令牌时，把旧的未用令牌一并作废：只有最新一封邮件里的链接有效，
    # 避免多封邮件并存导致「到底哪个链接能用」的困惑。
    for row in existing:
        row.used_at = now

    token = secrets.token_urlsafe(TOKEN_BYTES)
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_hash(token),
            expires_at=now + timedelta(minutes=settings.password_reset_expire_minutes),
        )
    )
    db.commit()
    return token


def resolve_token(
    db: OrmSession, token: Optional[str]
) -> Optional[Tuple[PasswordResetToken, User]]:
    """核验令牌：有效则返回 (令牌行, 用户)，否则返回 None。

    三种失败合并成同一个 None（路由返回同一句话）：
    - 令牌不存在（无效串 / 已被清理）；
    - 已被使用过（一次性）；
    - 已过期。
    """
    if not token:
        return None
    row = (
        db.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == _hash(token)
            )
        )
        .scalars()
        .first()
    )
    if row is None or row.used_at is not None or row.expires_at <= _now():
        return None

    user = db.get(User, row.user_id)
    if user is None or user.deleted_at is not None:
        return None
    return row, user


__all__ = ["REUSE_WINDOW_SECONDS", "TOKEN_BYTES", "issue_token", "resolve_token"]
