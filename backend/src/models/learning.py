# Models - 学习行为溯源
"""学习会话与对话记录（阶段 3 行为溯源）。

解决 Redis TTL 1h 导致的两件事：
- 对话历史过一小时就蒸发 → `chat_messages` 永久留存
- 学习进度（测验分数 / FSRS 复习计划）随会话过期丢失 → `learning_sessions` 留存

Redis 仍是热路径缓存（快），PostgreSQL 是真相源（不丢）。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class LearningSession(Base):
    """一次学习会话的完整生命周期（创建 → 讲解 → 测验 → 完成）"""

    __tablename__ = "learning_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 对外暴露的会话标识（sess_xxxxxxxx），与 Redis key 一致
    session_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 发起讲解的卡片 id（可能来自知识库，也可能来自内置示例）
    card_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    core_concepts: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    time_budget_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    preferred_depth: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")

    # 状态机状态：idle | pushed | learning | quiz | fsrs_update | completed
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="idle")
    # 本次会话生成的测验题组（含 correct_answer）。
    # 必须落库：否则 Redis 过期后回读的会话只剩分数，无法回答「这个分是怎么来的」，
    # quiz_score 也就成了不可审计的数字。
    quiz_questions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    quiz_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    fsrs_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fsrs_next_review_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<LearningSession {self.session_id} {self.state}>"


class ChatMessage(Base):
    """会话内的一条对话（user / assistant），永久留存"""

    __tablename__ = "chat_messages"
    # 主查询是「按会话取时间序」，因此建复合索引
    __table_args__ = (
        Index("ix_chat_messages_session_id_created_at", "session_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # user | assistant
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ChatMessage {self.session_id} {self.role}>"
