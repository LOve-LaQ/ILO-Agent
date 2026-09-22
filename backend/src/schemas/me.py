# ME Schemas - 个人中心契约
"""阶段 3 个人中心契约（/me/*）：把「我的」数据固化成对外契约。

- 收藏（/me/bookmarks）：服务端资产，替代浏览器 localStorage
- 学习记录（/me/sessions）：会话 + 对话历史，Redis 过期后仍可回看
- 行为时间线（/me/activities）：行为溯源的对外视图
- 个人概览（/me/profile）：账号信息 + 汇总统计

内容溯源（/discover/cards/{id}/provenance）是**公开**的卡片元数据，因此它的契约
放在 `src/schemas/discover.py`，不混进按用户划分的 /me 域。

所有列表接口都显式给出 `count` 与 `total`，前端不必靠 length 猜分页。
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from src.schemas.auth import UserPublic
from src.schemas.card import TechCard


# ==================== 个人中心概览 ====================


class LearningStats(BaseModel):
    """个人中心汇总数字"""

    sessions_total: int = 0
    sessions_completed: int = 0
    average_score: Optional[float] = Field(None, description="已完成会话的测验平均分")
    bookmarks_total: int = 0
    activities_total: int = 0


class MeProfileResponse(BaseModel):
    """GET /me/profile 响应"""

    user: UserPublic
    stats: LearningStats


# ==================== 收藏 ====================


class BookmarkItem(BaseModel):
    """一条收藏"""

    item_id: str
    created_at: datetime
    card: Optional[TechCard] = Field(
        None, description="卡片快照；知识库/缓存都取不到时为 null（收藏不因卡片失效而消失）"
    )


class BookmarkListResponse(BaseModel):
    """GET /me/bookmarks 响应"""

    items: List[BookmarkItem]
    count: int
    total: int
    item_ids: List[str] = Field(
        default_factory=list, description="全部收藏的卡片 id，前端一次拉取即可渲染所有卡片的收藏态"
    )
    source: str = Field(..., description="数据来源：postgres | unavailable")


class BookmarkMutationResponse(BaseModel):
    """POST / DELETE /me/bookmarks/{item_id} 响应"""

    item_id: str
    bookmarked: bool
    changed: bool = Field(..., description="本次调用是否真的改变了状态（幂等重复点击时为 false）")


# ==================== 学习记录 ====================


class SessionItem(BaseModel):
    """学习会话摘要（列表与详情共用）"""

    session_id: str
    card_id: Optional[str] = None
    topic: str
    summary: Optional[str] = None
    state: str = Field(..., description="idle | pushed | learning | quiz | fsrs_update | completed")
    quiz_score: Optional[float] = None
    fsrs_rating: Optional[int] = None
    fsrs_next_review_at: Optional[datetime] = None
    message_count: int = 0
    started_at: datetime
    completed_at: Optional[datetime] = None


class SessionListResponse(BaseModel):
    """GET /me/sessions 响应"""

    items: List[SessionItem]
    count: int
    total: int


class SessionMessage(BaseModel):
    """会话内一条对话（与 src/schemas/learning.py 的 ChatMessage 同构，但带时间）"""

    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class SessionDetailResponse(BaseModel):
    """GET /me/sessions/{session_id} 响应

    `messages` 是**分页后的一页**，`messages_total` 才是该会话的对话总数。
    两者都给出，前端才能区分「对话就这么长」与「还有下一页没取」，
    而不是把一页的长度当成事实。`session.message_count` 与 `messages_total` 同义，
    与列表接口的口径一致（都是总数）。
    """

    session: SessionItem
    messages: List[SessionMessage]
    messages_total: int = Field(..., description="该会话的对话总条数（不受分页影响）")
    has_more: bool = Field(..., description="是否还有下一页（offset + len(messages) < total）")


# ==================== 行为时间线 ====================


class ActivityItem(BaseModel):
    """一条行为流水"""

    action_type: str
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    request_id: Optional[str] = Field(None, description="与后端日志中的 request_id 一致，可对账")
    metadata: Optional[Dict[str, Any]] = None
    ip: Optional[str] = None
    created_at: datetime


class ActivityListResponse(BaseModel):
    """GET /me/activities 响应"""

    items: List[ActivityItem]
    count: int
    total: int


class DataExportResponse(BaseModel):
    """GET /me/export 导出内容结构。

    实际响应是 `Content-Disposition: attachment` 的 JSON 文件；这里给出 schema
    是为了让契约完整（每个接口都有明确 2xx schema），同时把「导出包含什么」写进
    接口契约里。
    """

    exported_at: str = Field(..., description="导出时间（ISO 8601）")
    format_version: str = Field(..., description="导出格式版本，便于将来兼容")
    profile: Dict[str, Any]
    bookmarks: List[Dict[str, Any]]
    learning_sessions: List[Dict[str, Any]]
    activities: List[Dict[str, Any]]
