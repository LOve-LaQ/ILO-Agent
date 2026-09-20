# Models - 邮件发件箱
"""email_outbox 表（阶段 5 邮件设施）。

**为什么要有一张发件箱表，而不是直接发完就算**：
1. 开发环境没有 SMTP 账号时，`console` 发信把邮件写在这里，测试与联调能直接从
   数据库取到重置链接，不必去翻日志；
2. 发信是「外部副作用」，失败原因（SMTP 拒信、超时）需要留痕，否则用户报
   「没收到邮件」时无从排查；
3. `purpose` 明确这封信是干什么的（reset / deletion…），保留期清理按用途区分。

**注意**：这张表会存邮件正文，正文里可能带一次性链接，因此**必须**配合保留期清理
（见 backend/retention_cleanup.py），不能无限留存。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, created_at_column


class EmailOutbox(Base):
    """一封待发 / 已发的邮件"""

    __tablename__ = "email_outbox"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    to_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 用途：password_reset | account_deletion | ...
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 状态：pending | sent | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<EmailOutbox {self.purpose} -> {self.to_email} ({self.status})>"
