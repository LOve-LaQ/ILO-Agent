# Services - 收藏（服务端资产）
"""用户收藏落 PostgreSQL（阶段 3 行为溯源）。

把收藏从浏览器 localStorage 搬到服务端，解决三件事：
1. 换设备 / 清缓存后收藏还在；
2. 收藏可统计（「被收藏最多的卡片」这种运营指标才有得算）；
3. 收藏是**行为溯源**的一部分，与 user_activities 里的 bookmark 流水互相印证。

与 learning/collection 服务不同，这里的失败**不吞掉**：收藏是用户显式动作，
静默失败会让用户以为「收藏成功了」却在下次打开时消失。
"""

import uuid
from typing import Any, Dict, List, Optional, Set, Union

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.db import get_session_factory, is_database_configured
from src.models.engagement import UserBookmark

UserId = Union[uuid.UUID, str, None]

MAX_ITEM_ID_LEN = 64


class BookmarkError(RuntimeError):
    """收藏操作失败（由路由层转成统一错误契约）"""


def _coerce_user_id(value: UserId) -> Optional[uuid.UUID]:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _require_uid(user_id: UserId) -> uuid.UUID:
    uid = _coerce_user_id(user_id)
    if uid is None:
        raise BookmarkError("收藏操作缺少有效用户标识。")
    return uid


def _require_db() -> None:
    if not is_database_configured():
        raise BookmarkError("收藏需要数据库支持，当前环境未配置 DATABASE_URL。")


def add_bookmark(user_id: UserId, item_id: str) -> bool:
    """收藏一张卡片；返回 True 表示本次新建，False 表示此前已收藏（幂等）"""
    _require_db()
    uid = _require_uid(user_id)
    item_id = (item_id or "").strip()
    if not item_id:
        raise BookmarkError("缺少要收藏的卡片 id。")
    if len(item_id) > MAX_ITEM_ID_LEN:
        raise BookmarkError("卡片 id 非法。")

    try:
        with get_session_factory()() as session:
            # ON CONFLICT DO NOTHING + RETURNING：一次往返同时拿到「是否新建」，
            # 且并发重复点击不会抛唯一约束错误
            stmt = (
                pg_insert(UserBookmark)
                .values(user_id=uid, item_id=item_id)
                .on_conflict_do_nothing(constraint="uq_user_bookmarks_user_id_item_id")
                .returning(UserBookmark.id)
            )
            created = session.execute(stmt).scalar_one_or_none() is not None
            # 必须显式提交：`with Session()` 退出时只 close，事务会被回滚 ——
            # 少了这一行，收藏会「报告成功但根本没存下来」，且重复点击永远返回新建。
            session.commit()
            return created
    except BookmarkError:
        raise
    except Exception as e:  # noqa: BLE001 - 转成业务错误，由路由层归一
        logger.error(f"❌ 收藏失败: {item_id} - {e}")
        raise BookmarkError("收藏失败，请稍后重试。")


def remove_bookmark(user_id: UserId, item_id: str) -> bool:
    """取消收藏；返回 True 表示确实删掉了一条（幂等）"""
    _require_db()
    uid = _require_uid(user_id)
    try:
        with get_session_factory()() as session:
            result = session.execute(
                delete(UserBookmark).where(
                    UserBookmark.user_id == uid, UserBookmark.item_id == item_id
                )
            )
            session.commit()
            return (result.rowcount or 0) > 0
    except Exception as e:  # noqa: BLE001
        logger.error(f"❌ 取消收藏失败: {item_id} - {e}")
        raise BookmarkError("取消收藏失败，请稍后重试。")


def is_bookmarked(user_id: UserId, item_id: str) -> bool:
    uid = _coerce_user_id(user_id)
    if not is_database_configured() or uid is None:
        return False
    try:
        with get_session_factory()() as session:
            return (
                session.execute(
                    select(UserBookmark.id).where(
                        UserBookmark.user_id == uid, UserBookmark.item_id == item_id
                    )
                ).scalar_one_or_none()
                is not None
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 收藏状态查询失败: {item_id} - {e}")
        return False


def get_bookmarked_ids(user_id: UserId) -> Set[str]:
    """取该用户全部收藏的卡片 id（前端一次拉取即可渲染所有卡片的收藏态）"""
    uid = _coerce_user_id(user_id)
    if not is_database_configured() or uid is None:
        return set()
    try:
        with get_session_factory()() as session:
            rows = session.execute(
                select(UserBookmark.item_id).where(UserBookmark.user_id == uid)
            ).scalars()
            return {row for row in rows}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 收藏 id 列表读取失败: {e}")
        return set()


def list_bookmarks(
    user_id: UserId, limit: int = 50, offset: int = 0
) -> List[Dict[str, Any]]:
    """按收藏时间倒序取收藏列表"""
    uid = _coerce_user_id(user_id)
    if not is_database_configured() or uid is None:
        return []
    try:
        with get_session_factory()() as session:
            rows = session.execute(
                select(UserBookmark)
                .where(UserBookmark.user_id == uid)
                .order_by(UserBookmark.created_at.desc())
                .limit(max(1, min(limit, 200)))
                .offset(max(0, offset))
            ).scalars()
            return [
                {"item_id": row.item_id, "created_at": row.created_at} for row in rows
            ]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 收藏列表读取失败: {e}")
        return []


def count_bookmarks(user_id: UserId) -> int:
    uid = _coerce_user_id(user_id)
    if not is_database_configured() or uid is None:
        return 0
    try:
        with get_session_factory()() as session:
            return int(
                session.execute(
                    select(func.count(UserBookmark.id)).where(UserBookmark.user_id == uid)
                ).scalar()
                or 0
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 收藏统计失败: {e}")
        return 0


__all__ = [
    "BookmarkError",
    "add_bookmark",
    "count_bookmarks",
    "get_bookmarked_ids",
    "is_bookmarked",
    "list_bookmarks",
    "remove_bookmark",
]
