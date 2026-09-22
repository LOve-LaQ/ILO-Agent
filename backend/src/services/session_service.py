# Session Service - 会话生命周期
"""阶段 5 会话可撤销：会话表的读写 + refresh 轮换 + reuse 检测 + sid 黑名单。

设计主线（配合 models/session.py 与 api/routes/auth.py）：

1. **一个会话一行**：登录/注册时为每台设备建一行 `user_sessions`，refresh token 的
   `jti` 落在这行上；「撤销某设备」= 把这行置 `revoked_at`。
2. **refresh 轮换**：每次刷新都换发新 refresh、作废旧行并串成链（`replaced_by_jti`）。
3. **reuse 检测**：一个**已作废**的 refresh 再次出现，有三种可能 —— 该会话被显式
   撤销（登出/下线设备/改密，正常）、多标签页并发（正常）、token 被盗（异常）。
   先按 `replaced_by_jti` 是否为 None 排除「显式撤销」；余下的用「宽限窗 + 替换会话
   是否仍活跃」区分：窗内且替换会话活跃 → 只补发 access；否则 → 判为盗用，撤销该
   用户全部会话。**不能把「显式撤销」也当盗用**，否则用户一登出就会收到「账号有
   登录异常」。
4. **sid 黑名单**：撤销会话时把 `sid` 写进 Redis（TTL = access 有效期），
   让该会话的 access **立即** 401，而不是等它自然过期（最长 30 分钟）。

**降级取向**：Redis 不可用时，sid 黑名单退化为「无」——即撤销对 access 的立即生效
失效，但 refresh 侧的撤销（走数据库）依然精确。这只影响单机本地环境，生产必须保证
Redis 可用。
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from src.core.config import settings
from src.core.errors import ILOException
from src.core.net import client_ip, hash_ip
from src.core.redis_client import get_redis
from src.core.security import create_token
from src.models.session import UserSession
from src.models.user import User

# Redis 键前缀：记录「已撤销会话的 sid」，TTL 取 access 有效期即可
_SID_KEY_PREFIX = "revoked:sid"

# UA 是技术标识，可保留，但截断超长垃圾（与 activity_service 口径一致）
MAX_USER_AGENT_LENGTH = 256


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sid_key(session_id: str) -> str:
    return f"{_SID_KEY_PREFIX}:{session_id}"


def _sid_ttl() -> int:
    # 黑名单只需覆盖 access token 的剩余寿命：access 一过期，sid 是否在册已无意义
    return max(60, settings.access_token_expire_minutes * 60)


def _user_agent(request) -> Optional[str]:
    if request is None:
        return None
    raw = request.headers.get("user-agent")
    return raw[:MAX_USER_AGENT_LENGTH] if raw else None


# ---------------------------------------------------------------- sid 黑名单


def blacklist_sid(session_id: Optional[str]) -> None:
    """把会话 sid 记入黑名单，使该会话的 access token 立即失效"""
    if not session_id:
        return
    client = get_redis()
    if client is None:
        return
    try:
        # 用 set(ex=...) 而不是已弃用的 setex
        client.set(_sid_key(session_id), "1", ex=_sid_ttl())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 写入会话黑名单失败（撤销将延迟到 access 过期）: {e}")


def is_sid_revoked(session_id: Optional[str]) -> bool:
    """会话是否已被撤销（Redis 不可用时返回 False，退化为「最多 30 分钟窗口」）"""
    if not session_id:
        return False
    client = get_redis()
    if client is None:
        return False
    try:
        return bool(client.exists(_sid_key(session_id)))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 查询会话黑名单失败，按未撤销处理: {e}")
        return False


# ---------------------------------------------------------------- 建会话 / 轮换


def start_session(
    db: OrmSession, user: User, request=None
) -> tuple[str, str, datetime]:
    """新建一个会话并签发 access + refresh。

    返回 (access_token, refresh_token, refresh 过期时间)。sid 取会话主键，
    同时写进两个 token：access 带它用于黑名单即时失效，refresh 带它用于定位会话。
    """
    session_id = uuid.uuid4()
    refresh_jti = uuid.uuid4().hex
    now = _now()

    access_token, _ = create_token(str(user.id), "access", sid=str(session_id))
    refresh_token, refresh_expires_at = create_token(
        str(user.id), "refresh", jti=refresh_jti, sid=str(session_id)
    )

    db.add(
        UserSession(
            id=session_id,
            user_id=user.id,
            refresh_jti=refresh_jti,
            issued_at=now,
            expires_at=refresh_expires_at,
            ip_hash=hash_ip(client_ip(request)),
            user_agent=_user_agent(request),
            last_seen_at=now,
        )
    )
    db.commit()
    return access_token, refresh_token, refresh_expires_at


def _rotate(
    db: OrmSession, user: User, session: UserSession, request=None
) -> dict:
    """正常轮换：作废旧行、串链接、签发新会话、立即失效旧 sid"""
    now = _now()
    new_session_id = uuid.uuid4()
    new_jti = uuid.uuid4().hex

    access_token, _ = create_token(str(user.id), "access", sid=str(new_session_id))
    refresh_token, refresh_expires_at = create_token(
        str(user.id), "refresh", jti=new_jti, sid=str(new_session_id)
    )

    session.revoked_at = now
    session.replaced_by_jti = new_jti
    db.add(
        UserSession(
            id=new_session_id,
            user_id=user.id,
            refresh_jti=new_jti,
            issued_at=now,
            expires_at=refresh_expires_at,
            ip_hash=hash_ip(client_ip(request)),
            user_agent=_user_agent(request),
            last_seen_at=now,
        )
    )
    db.commit()

    # 旧 sid 立即失效：旧 access 不必等到自然过期
    blacklist_sid(str(session.id))

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "refresh_expires_at": refresh_expires_at,
        "sid": str(new_session_id),
    }


def _find_by_jti(db: OrmSession, jti: str) -> Optional[UserSession]:
    return (
        db.execute(select(UserSession).where(UserSession.refresh_jti == jti))
        .scalars()
        .first()
    )


def rotate_session(
    db: OrmSession, *, user: User, jti: Optional[str], request=None
) -> dict:
    """用 refresh 的 jti 轮换会话；含 reuse 检测。

    返回 dict：`refresh_token` 为 None 表示「只补发 access」（并发宽限路径）。
    异常路径抛 ILOException（401）。
    """
    if not jti:
        raise ILOException("TOKEN_INVALID", "登录凭证无效，请重新登录。", status_code=401)

    session = _find_by_jti(db, jti)
    if session is None or session.user_id != user.id:
        # 查不到（已被清理）或与 token 的 sub 不符：一律视为无效凭证
        raise ILOException("TOKEN_INVALID", "登录凭证无效，请重新登录。", status_code=401)

    if session.revoked_at is None:
        return _rotate(db, user, session, request)

    return _handle_reuse(db, user, session)


def _handle_reuse(db: OrmSession, user: User, session: UserSession) -> dict:
    """已作废 refresh 再次出现：区分「显式撤销」「并发」与「盗用」"""
    now = _now()
    grace = max(0, settings.session_rotate_grace_seconds)
    age = (now - session.revoked_at).total_seconds()
    replacement = (
        _find_by_jti(db, session.replaced_by_jti)
        if session.replaced_by_jti
        else None
    )

    if session.replaced_by_jti is None:
        # 【这一支是「自己登出却被判盗用」的根源】没有 replaced_by_jti，说明这行
        # 不是被轮换掉的，而是被**显式撤销**的：登出、下线设备、改密、申请注销。
        # 这类 token 会被合法客户端继续持有 —— 登出响应体与清除 Cookie 之间存在
        # 竞态，多标签页可能有在途刷新请求，离线重连的客户端还会重放它。
        # 把这些一律判为盗用并 `revoke_all_sessions`，用户看到的就是
        # 「我刚登出，却被提示账号有登录异常、全部设备已退出」。
        # 只有「本该只存在一份的 token 出现了第二份」才是盗用证据，而那种情形
        # 必然带 replaced_by_jti（见 _rotate）。所以这里按普通凭证失效处理。
        logger.info(
            f"显式撤销的 refresh token 再次出现，按凭证失效处理: session={session.id}"
        )
        raise ILOException(
            "TOKEN_INVALID", "登录状态已失效，请重新登录。", status_code=401
        )

    if age <= grace and replacement is not None and replacement.revoked_at is None:
        # 多标签页共享同一 Cookie、几乎同时刷新：正常情形。
        # 只补发一个绑定到「当前活跃会话」的 access，不再下发新的 refresh。
        access_token, _ = create_token(str(user.id), "access", sid=str(replacement.id))
        return {
            "access_token": access_token,
            "refresh_token": None,
            "refresh_expires_at": None,
            "sid": str(replacement.id),
        }

    # 超出宽限窗，或替换会话也已作废 —— 判为 token 盗用：撤销该用户全部会话
    revoked = revoke_all_sessions(db, user.id)
    logger.warning(
        f"[WARN] 检测到 refresh token 重复使用（reuse），已撤销用户会话数={revoked}"
    )
    raise ILOException(
        "TOKEN_REUSE",
        "登录状态异常，出于安全考虑已退出全部设备，请重新登录。",
        status_code=401,
    )


# ---------------------------------------------------------------- 撤销与查询


def revoke_session(db: OrmSession, session: UserSession) -> None:
    """撤销单个会话（幂等）"""
    if session.revoked_at is not None:
        return
    session.revoked_at = _now()
    db.commit()
    blacklist_sid(str(session.id))


def revoke_by_jti(db: OrmSession, jti: Optional[str]) -> bool:
    """按 refresh 的 jti 撤销会话（登出时用）。返回是否命中了活跃会话。"""
    if not jti:
        return False
    session = _find_by_jti(db, jti)
    if session is None:
        return False
    revoke_session(db, session)
    return True


def revoke_all_sessions(
    db: OrmSession, user_id: uuid.UUID, *, except_sid: Optional[str] = None
) -> int:
    """撤销某用户的全部活跃会话（可排除一个 sid），返回撤销数量。

    用于：盗用检测、改密码、重置密码、申请注销。改密码场景传 `except_sid`
    排除当前会话，避免「改完密码把自己也踢下线」。
    """
    rows = (
        db.execute(
            select(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    now = _now()
    count = 0
    for row in rows:
        if except_sid and str(row.id) == except_sid:
            continue
        row.revoked_at = now
        blacklist_sid(str(row.id))
        count += 1
    db.commit()
    return count


def list_active_sessions(db: OrmSession, user_id: uuid.UUID) -> list[UserSession]:
    """列出尚未撤销且未过期的会话（按最后活跃倒序）"""
    now = _now()
    return list(
        db.execute(
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
            )
            .order_by(UserSession.last_seen_at.desc().nullslast())
        )
        .scalars()
        .all()
    )


def get_session_for_user(
    db: OrmSession, user_id: uuid.UUID, session_id: uuid.UUID
) -> Optional[UserSession]:
    """取属于该用户的会话（越权时调用方应返回 404，不泄露会话是否存在）"""
    return (
        db.execute(
            select(UserSession).where(
                UserSession.id == session_id,
                UserSession.user_id == user_id,
            )
        )
        .scalars()
        .first()
    )


def touch_last_seen(db: OrmSession, session: UserSession) -> None:
    """更新最后活跃时间（best-effort，失败了也不影响请求）"""
    try:
        session.last_seen_at = _now()
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 更新会话活跃时间失败: {e}")


__all__ = [
    "blacklist_sid",
    "get_session_for_user",
    "is_sid_revoked",
    "list_active_sessions",
    "revoke_all_sessions",
    "revoke_by_jti",
    "revoke_session",
    "rotate_session",
    "start_session",
    "touch_last_seen",
]
