# Me Routes - 个人中心 API
"""个人中心（阶段 3 可追溯链路）：把「我的」数据变成服务端资产。

- GET    /me/profile            : 用户信息 + 汇总统计
- GET    /me/bookmarks          : 收藏列表（含卡片快照）
- POST   /me/bookmarks/{item_id}: 收藏（幂等）
- DELETE /me/bookmarks/{item_id}: 取消收藏（幂等）
- GET    /me/sessions           : 学习记录列表
- GET    /me/sessions/{id}      : 学习记录详情（含对话历史）
- GET    /me/activities         : 行为时间线

契约：
- 全部要求登录（权限边界：只返回 `current_user` 自己的数据，查询条件里就带 user_id，
  不依赖调用方记得过滤）
- 收藏的写操作失败会**明确报错**（这是用户的显式动作，静默失败等于骗人）；
  其余读取接口在数据库不可用时返回空列表，让页面优雅降级
"""

from typing import Optional

import asyncio
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Query, Request, Response, status

from src.api.deps import CurrentUser
from src.core.db import is_database_configured
from src.core.errors import ERROR_RESPONSES, ILOException
from src.models.engagement import ACTION_TYPES
from src.schemas.auth import UserPublic
from src.schemas.me import (
    ActivityItem,
    ActivityListResponse,
    BookmarkItem,
    BookmarkListResponse,
    BookmarkMutationResponse,
    DataExportResponse,
    LearningStats,
    MeProfileResponse,
    SessionDetailResponse,
    SessionItem,
    SessionListResponse,
    SessionMessage,
)
from src.services.activity_service import (
    count_activities,
    list_activities,
    log_activity_async,
)
from src.services.bookmark_service import (
    BookmarkError,
    add_bookmark,
    count_bookmarks,
    get_bookmarked_ids,
    list_bookmarks,
    remove_bookmark,
)
from src.services.card_service import find_cards
from src.services.learning_service import (
    count_messages,
    count_messages_by_session,
    get_session,
    list_messages,
    list_sessions,
    session_stats,
)

router = APIRouter(prefix="/me", tags=["Me"], responses=ERROR_RESPONSES)

# 数据库不可用时的统一话术：调用方能从中知道「不是自己操作错了」
_DB_UNAVAILABLE_MESSAGE = "当前环境未配置数据库，该功能不可用。"


def _ensure_db_available() -> None:
    if not is_database_configured():
        raise ILOException("DATABASE_UNAVAILABLE", _DB_UNAVAILABLE_MESSAGE, status_code=503)


# ==================== 概览 ====================


@router.get("/profile", response_model=MeProfileResponse)
async def get_my_profile(user: CurrentUser) -> MeProfileResponse:
    """个人中心概览：账号信息 + 学习/收藏/行为汇总"""
    # 三条统计都是同步 DB 查询且互不依赖，并发卸载 —— 串行三次往返会白等两个 RTT
    stats, bookmarks_total, activities_total = await asyncio.gather(
        asyncio.to_thread(session_stats, user.id),
        asyncio.to_thread(count_bookmarks, user.id),
        asyncio.to_thread(count_activities, user.id),
    )
    return MeProfileResponse(
        user=UserPublic.from_user(user),
        stats=LearningStats(
            sessions_total=stats["sessions_total"],
            sessions_completed=stats["sessions_completed"],
            average_score=stats["average_score"],
            bookmarks_total=bookmarks_total,
            activities_total=activities_total,
        ),
    )


# ==================== 收藏 ====================


@router.get("/bookmarks", response_model=BookmarkListResponse)
async def get_my_bookmarks(
    user: CurrentUser,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> BookmarkListResponse:
    """收藏列表：卡片快照顺带返回，前端不必再逐个查卡片"""
    rows = await asyncio.to_thread(list_bookmarks, user.id, limit=limit, offset=offset)
    cards = await asyncio.to_thread(find_cards, [row["item_id"] for row in rows])

    items = [
        BookmarkItem(
            item_id=row["item_id"],
            created_at=row["created_at"],
            # 卡片查不到（被清理 / 知识库不可用）时留 None：收藏本身不能因此消失
            card=cards.get(row["item_id"]),
        )
        for row in rows
    ]
    all_ids = sorted(await asyncio.to_thread(get_bookmarked_ids, user.id))
    total = await asyncio.to_thread(count_bookmarks, user.id)
    return BookmarkListResponse(
        items=items,
        count=len(items),
        total=total,
        item_ids=all_ids,
        # 按「数据库是否配置」判定，而不是「有没有收藏」——
        # 否则一条收藏都没有时会误报 unavailable，前端据此提示「存储不可用」就错怪了环境。
        source="postgres" if is_database_configured() else "unavailable",
    )


@router.post(
    "/bookmarks/{item_id}",
    response_model=BookmarkMutationResponse,
    status_code=status.HTTP_200_OK,
)
async def create_bookmark(
    request: Request, item_id: str, user: CurrentUser
) -> BookmarkMutationResponse:
    """收藏一张卡片（重复收藏幂等，`changed=false`）"""
    _ensure_db_available()
    try:
        created = await asyncio.to_thread(add_bookmark, user.id, item_id)
    except BookmarkError as e:
        raise ILOException("BOOKMARK_FAILED", str(e), status_code=503)

    if created:
        await log_activity_async(
            "bookmark",
            user_id=user.id,
            target_type="card",
            target_id=item_id,
            request=request,
        )
    return BookmarkMutationResponse(item_id=item_id, bookmarked=True, changed=created)


@router.delete("/bookmarks/{item_id}", response_model=BookmarkMutationResponse)
async def delete_bookmark(
    request: Request, item_id: str, user: CurrentUser
) -> BookmarkMutationResponse:
    """取消收藏（没收藏过也返回成功，`changed=false`）"""
    _ensure_db_available()
    try:
        removed = await asyncio.to_thread(remove_bookmark, user.id, item_id)
    except BookmarkError as e:
        raise ILOException("BOOKMARK_FAILED", str(e), status_code=503)

    if removed:
        await log_activity_async(
            "unbookmark",
            user_id=user.id,
            target_type="card",
            target_id=item_id,
            request=request,
        )
    return BookmarkMutationResponse(item_id=item_id, bookmarked=False, changed=removed)


# ==================== 学习记录 ====================


@router.get("/sessions", response_model=SessionListResponse)
async def get_my_sessions(
    user: CurrentUser,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    state: Optional[str] = Query(None, description="按状态过滤：completed 等"),
) -> SessionListResponse:
    """学习记录列表（Redis 过期后依然可回看，因为真相源在 PostgreSQL）"""
    rows = await asyncio.to_thread(
        list_sessions, user.id, limit=limit, offset=offset, state=state
    )
    session_ids = [row["session_id"] for row in rows]

    # 两条聚合查询都只依赖 rows、互不依赖，并发卸载
    counts, stats = await asyncio.gather(
        asyncio.to_thread(count_messages_by_session, session_ids),
        asyncio.to_thread(session_stats, user.id),
    )
    items = [
        SessionItem(**row, message_count=counts.get(row["session_id"], 0)) for row in rows
    ]
    return SessionListResponse(
        items=items,
        count=len(items),
        total=stats["sessions_total"],
    )


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_my_session_detail(
    session_id: str,
    user: CurrentUser,
    limit: int = Query(200, ge=1, le=500, description="本页最多返回多少条对话"),
    offset: int = Query(0, ge=0, description="从第几条对话开始（按时间正序）"),
) -> SessionDetailResponse:
    """学习记录详情：会话进度 + 对话历史（分页）

    对话此前固定在 200 条、超出即静默丢弃。现在 limit/offset 由调用方控制，
    并用 `messages_total` + `has_more` 明确告知是否还有下一页 ——
    截断要么不给，要给就必须让调用方知道。
    """
    row = await asyncio.to_thread(get_session, session_id, user.id)
    if row is None:
        # 不存在与不属于当前用户都返回 404：不泄露「这个 session_id 是否存在」
        raise ILOException("SESSION_NOT_FOUND", "学习记录不存在。", status_code=404)

    messages, total = await asyncio.gather(
        asyncio.to_thread(list_messages, session_id, limit=limit, offset=offset),
        asyncio.to_thread(count_messages, session_id),
    )
    return SessionDetailResponse(
        # 与列表接口口径一致：message_count 是总数，不是本页条数
        session=SessionItem(**row, message_count=total),
        messages=[SessionMessage(**m) for m in messages],
        messages_total=total,
        has_more=offset + len(messages) < total,
    )


# ==================== 行为时间线 ====================


@router.get("/activities", response_model=ActivityListResponse)
async def get_my_activities(
    user: CurrentUser,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    action_type: Optional[str] = Query(
        None, description="按动作过滤，取值见 ACTION_TYPES：" + " | ".join(ACTION_TYPES)
    ),
) -> ActivityListResponse:
    """行为时间线（行为溯源）：每条都带 request_id，可与后端日志对账"""
    if action_type and action_type not in ACTION_TYPES:
        raise ILOException(
            "INVALID_ACTION_TYPE",
            f"未知的动作类型：{action_type}。",
            status_code=400,
        )

    rows, total = await asyncio.gather(
        asyncio.to_thread(
            list_activities, user.id, limit=limit, offset=offset, action_type=action_type
        ),
        asyncio.to_thread(count_activities, user.id),
    )
    return ActivityListResponse(
        items=[ActivityItem(**row) for row in rows],
        count=len(rows),
        total=total,
    )


# ==================== 数据导出（数据权） ====================


# 导出是「一次性全量」场景，不像列表页要分页；设一个上限防止超大账号把响应撑爆内存
_EXPORT_ROW_LIMIT = 5000

# filename 是**响应头**，而 username 是用户可控字段。若把它原样拼进头部：
#   1. 用户名里的 `"`、`\r`、`\n` 能直接截断/伪造响应头（HTTP 响应头注入）；
#   2. 中文等非 ASCII 字符会让部分老客户端解析失败甚至丢弃整个文件。
# 所以只把「白名单内」的 ASCII 字符放进 `filename=`（给老客户端兜底），
# 真实名字用 RFC 5987 的 `filename*=UTF-8''` 走百分号编码承载。
_ASCII_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_MAX_FILENAME_BASE = 64


def _attachment_disposition(base_name: str, extension: str = ".json") -> str:
    """安全构造附件下载的 Content-Disposition 头。

    - ASCII 回退名：白名单外的字符一律替换为 `_`，并截断长度，保证头部结构不被破坏
    - UTF-8 名：先截断再整体百分号编码（`quote` 会把 CR/LF 也编码成 `%0D/%0A`，
      注入字符被彻底中和），用 `filename*` 承载真实的（可能含中文的）用户名
    """
    base_name = (base_name or "").strip()[:_MAX_FILENAME_BASE]
    ascii_base = _ASCII_FILENAME_UNSAFE.sub("_", base_name).strip("._") or "user"
    utf8_name = quote(f"{base_name}{extension}", safe="")
    return (
        f'attachment; filename="{ascii_base}{extension}"; '
        f"filename*=UTF-8''{utf8_name}"
    )


@router.get("/export", response_model=DataExportResponse)
async def export_my_data(request: Request, user: CurrentUser) -> Response:
    """导出当前用户的全部数据（JSON 附件）

    覆盖：账号资料 + 收藏 + 学习会话（含对话）+ 行为记录。这是「数据可携带」的
    最低要求 —— 用户能把自己贡献的数据完整带走，而不是被锁在服务端。
    """
    _ensure_db_available()

    from src.services.bookmark_service import list_bookmarks

    profile = UserPublic.from_user(user).model_dump(mode="json")

    # 三条全量查询互不依赖，并发卸载。导出是「一次性拉全量」，每条上限 5000 行，
    # 串行会让事件循环在三次查询的总时长里完全无法服务其他请求。
    bookmarks, session_rows, activities = await asyncio.gather(
        asyncio.to_thread(list_bookmarks, user.id, limit=_EXPORT_ROW_LIMIT, offset=0),
        asyncio.to_thread(list_sessions, user.id, limit=_EXPORT_ROW_LIMIT, offset=0),
        asyncio.to_thread(list_activities, user.id, limit=_EXPORT_ROW_LIMIT, offset=0),
    )
    bookmark_items = [
        {"item_id": row["item_id"], "created_at": row["created_at"]} for row in bookmarks
    ]

    # 每个会话的对话单独查一次（N+1）。这里保持 N 次查询而不改写成一条 IN 查询：
    # 导出是低频操作，改查询语义的收益抵不过风险；但每次查询都卸载到线程，
    # 保证这一串同步查询不会占住事件循环。
    sessions: list[dict] = []
    for row in session_rows:
        sessions.append(
            {
                **row,
                "messages": await asyncio.to_thread(list_messages, row["session_id"]),
            }
        )

    export_payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "format_version": "1.0",
        "profile": profile,
        "bookmarks": bookmark_items,
        "learning_sessions": sessions,
        "activities": activities,
    }
    body = json.dumps(export_payload, ensure_ascii=False, indent=2, default=str)

    await log_activity_async(
        "export_data",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        request=request,
    )

    filename = f"ilo-export-{user.username}"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": _attachment_disposition(filename)},
    )
