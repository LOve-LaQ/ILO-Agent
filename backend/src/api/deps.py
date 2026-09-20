# Deps - FastAPI 依赖
"""
阶段 2 权限边界：数据库会话与当前用户解析。

- `get_db`: 每请求一个 Session（转发 core/db.py）
- `get_current_user`: 从 `Authorization: Bearer <access token>` 解析用户。
  未登录 / token 过期 / token 类型不符一律 401，并用不同 code 区分，
  前端据此决定「静默 refresh 重放」还是「跳转登录」。
"""

import uuid
from typing import Annotated, Optional

import jwt
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from src.core.db import get_db, get_optional_db
from src.core.errors import ILOException
from src.core.security import decode_token
from src.models.user import User


def _extract_bearer_token(request: Request) -> Optional[str]:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """解析当前登录用户；任何凭证问题都归一为 401 业务错误"""
    token = _extract_bearer_token(request)
    if token is None:
        raise ILOException("NOT_AUTHENTICATED", "请先登录后再访问该功能。", status_code=401)

    try:
        payload = decode_token(token, expected_type="access")
    except jwt.ExpiredSignatureError:
        raise ILOException("TOKEN_EXPIRED", "登录状态已过期，请重新登录。", status_code=401)
    except jwt.PyJWTError:
        raise ILOException("TOKEN_INVALID", "登录凭证无效，请重新登录。", status_code=401)

    # 会话撤销即时生效：被撤销会话（改密/下线/盗用）的 sid 在黑名单里，
    # 命中即 401，不必等 access 自然过期。Redis 不可用时该检查退化为「无」。
    from src.services.session_service import is_sid_revoked

    if is_sid_revoked(payload.get("sid")):
        raise ILOException(
            "SESSION_REVOKED", "登录状态已失效，请重新登录。", status_code=401
        )

    try:
        user_id = uuid.UUID(str(payload.get("sub")))
    except (TypeError, ValueError):
        raise ILOException("TOKEN_INVALID", "登录凭证无效，请重新登录。", status_code=401)

    user = db.get(User, user_id)
    if user is None:
        raise ILOException("USER_NOT_FOUND", "账号不存在，请重新登录。", status_code=401)
    if not user.is_active:
        raise ILOException("USER_DISABLED", "账号已被停用。", status_code=403)
    return user


def get_optional_user(
    request: Request, db: Optional[Session] = Depends(get_optional_db)
) -> Optional[User]:
    """解析当前用户但**不强制**。

    用于两类接口：
    - logout：access token 已过期时用户依然必须能登出，否则前端会卡在
      「登不出去、又提示要重新登录」的死角；
    - `/discover/*` 这类匿名可访问的公开接口：顺手记录「谁触发的」，
      未登录或未配置数据库时退化为 None，绝不能因此 500。
    """
    if db is None:
        return None
    try:
        return get_current_user(request, db)
    except ILOException:
        return None


# 路由签名直接用这些别名，避免每个函数重复写 Depends(...)
CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[Optional[User], Depends(get_optional_user)]
DbSession = Annotated[Session, Depends(get_db)]
OptionalDbSession = Annotated[Optional[Session], Depends(get_optional_db)]
