# Models - 用户资产与行为流水
"""收藏与行为流水（阶段 3 行为溯源）。

- `user_bookmarks`：收藏从浏览器 localStorage 迁到服务端，跨设备一致、可统计
- `user_activities`：一条动作一条流水（register/login/bookmark/start_session/...），
  带 request_id 可与日志对账，带 ip/user_agent 可做安全审计
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base

# 受控动作词表：埋点与查询共用，避免各处拼错字符串
ACTION_TYPES = (
    "register",
    "login",
    "logout",
    "view_card",
    "bookmark",
    "unbookmark",
    "start_session",
    "push_notification",
    "process_response",
    "ask_question",
    "submit_quiz",
    "complete_session",
    # 阶段 5 账号生命周期与安全事件
    "password_reset",
    "password_change",
    "account_delete_requested",
    "account_delete_cancelled",
    "account_deleted",
    "session_revoked",
    "security_captcha_failed",
    "security_token_reuse",
    "email_verified",
    "export_data",
)


class UserBookmark(Base):
    """用户收藏的技术卡片（(user_id, item_id) 唯一，重复收藏幂等）"""

    __tablename__ = "user_bookmarks"
    __table_args__ = (
        UniqueConstraint("user_id", "item_id", name="uq_user_bookmarks_user_id_item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<UserBookmark {self.user_id} {self.item_id}>"


class UserActivity(Base):
    """行为流水（时间线查询：按用户 + 时间倒序）"""

    __tablename__ = "user_activities"
    # 时间线查询是「按 user_id 过滤 + created_at 倒序」；PG 的 btree 索引可反向扫描，
    # 因此不必显式声明 DESC，普通复合索引即可
    __table_args__ = (
        Index("ix_user_activities_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 允许为空：未登录时的 view_card 也要能记录，否则行为链会在登录前断掉
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    action_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 请求链路标识：与 loguru 日志中的 request_id 一致，便于对账排查。
    # 宽度给 64 而不是 UUID 的 36：上游网关的 request id 往往更长，
    # 一旦超宽 psycopg2 会抛 StringDataRightTruncation，而埋点异常是被吞掉的
    # —— 结果是审计流水静默丢失，比报错更危险。
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 属性名不能叫 metadata（与 SQLAlchemy Declarative 的 metadata 冲突），列名保持 metadata
    meta: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    # 属性名用 ip_hash 标注「这里存的是 HMAC 指纹，不是明文 IP」，但列名保持 ip
    # 不改（SQLAlchemy 允许属性名与列名不同，meta/metadata 已是先例）：
    # 改名会牵动一条无谓的列重命名迁移，而数据本身的语义才是要表达的。
    ip_hash: Mapped[str | None] = mapped_column("ip", String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<UserActivity {self.action_type} {self.target_id}>"
