# Services - 行为埋点
"""行为溯源埋点（阶段 3）。

三条约定（都是为了「埋点不能拖累业务」）：
1. **永不抛出**：审计/统计失败绝不能把用户正常操作带崩；
2. **独立短事务**：不复用请求会话，避免埋点的 commit/rollback 干扰主流程事务；
3. **request_id 走 contextvars**：与 loguru 日志共用同一个 id，出问题能对上账。
"""

import uuid
from typing import Any, Dict, List, Optional, Union

from fastapi import Request
from loguru import logger
from sqlalchemy import func, select

from src.core.context import get_request_id
from src.core.db import get_session_factory, is_database_configured
from src.core.net import client_ip, hash_ip
from src.models.engagement import UserActivity

UserId = Union[uuid.UUID, str, None]

# user_agent 只做技术标识，不是业务数据：截断到 256 字符，避免超长垃圾把行撑大
# （有些爬虫/junk UA 会塞进几 KB）。这既是存储卫生，也缩小 PII 面积。
MAX_USER_AGENT_LENGTH = 256


def _coerce_user_id(value: UserId) -> Optional[uuid.UUID]:
    """容忍 str / UUID / None：埋点调用点不必各自做转换"""
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _ip_hash(request: Request) -> Optional[str]:
    """落库用的 IP 指纹（HMAC）。绝不留明文 —— 明文 IP 属个人信息，
    库被拖走即泄露访问者网络位置。未配 `IP_HASH_SECRET` 时返回 None（宁可不记）。"""
    return hash_ip(client_ip(request))


def _user_agent(request: Request) -> Optional[str]:
    """截断后的 UA（技术标识，保留；但拒绝超长垃圾入库）"""
    raw = request.headers.get("user-agent")
    if not raw:
        return None
    return raw[:MAX_USER_AGENT_LENGTH]


def log_activity(
    action_type: str,
    *,
    user_id: UserId = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    request: Optional[Request] = None,
) -> None:
    """记一条行为流水（best-effort，永不抛出）

    注意：调用前需保证 `user_id` 对应的用户已提交，否则外键会失败
    （注册/登录这类「先写用户再埋点」的流程里，用户已在请求事务中 commit）。
    """
    if not is_database_configured():
        return
    try:
        with get_session_factory()() as session:
            session.add(
                UserActivity(
                    user_id=_coerce_user_id(user_id),
                    action_type=action_type,
                    target_type=target_type,
                    target_id=target_id,
                    request_id=get_request_id(),
                    meta=metadata,
                    ip_hash=_ip_hash(request) if request is not None else None,
                    user_agent=_user_agent(request) if request is not None else None,
                )
            )
            session.commit()
    except Exception as e:  # noqa: BLE001 - 埋点必须吞掉所有异常
        logger.warning(f"[WARN] 行为埋点失败（不影响主流程）: {action_type} - {e}")


def _brief(row: UserActivity) -> Dict[str, Any]:
    return {
        "action_type": row.action_type,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "request_id": row.request_id,
        "metadata": row.meta,
        # 对外字段名仍叫 ip（契约稳定），值已是 HMAC 指纹
        "ip": row.ip_hash,
        "created_at": row.created_at,
    }


def list_activities(
    user_id: UserId,
    *,
    limit: int = 50,
    offset: int = 0,
    action_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """按时间倒序取某个用户的行为时间线（只按 user_id 过滤，天然隔离他人数据）"""
    uid = _coerce_user_id(user_id)
    if not is_database_configured() or uid is None:
        return []
    try:
        with get_session_factory()() as session:
            stmt = (
                select(UserActivity)
                .where(UserActivity.user_id == uid)
                .order_by(UserActivity.created_at.desc())
                .limit(max(1, min(limit, 200)))
                .offset(max(0, offset))
            )
            if action_type:
                stmt = stmt.where(UserActivity.action_type == action_type)
            return [_brief(row) for row in session.execute(stmt).scalars()]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 行为时间线读取失败: {e}")
        return []


def count_activities(user_id: UserId) -> int:
    uid = _coerce_user_id(user_id)
    if not is_database_configured() or uid is None:
        return 0
    try:
        with get_session_factory()() as session:
            return int(
                session.execute(
                    select(func.count(UserActivity.id)).where(
                        UserActivity.user_id == uid
                    )
                ).scalar()
                or 0
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 行为流水统计失败: {e}")
        return 0


__all__ = ["count_activities", "list_activities", "log_activity"]
