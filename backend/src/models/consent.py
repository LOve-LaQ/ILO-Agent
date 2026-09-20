# Models - 隐私同意记录
"""consent_records 表（阶段 5 隐私与信息保护）。

**为什么记「版本」而不是一个布尔值**：隐私政策与用户协议会改版，改版后旧版本下
获得的同意是否需要重新获取、当时到底同意了哪一版，是合规上必须能回答的问题。
只存一个 `agreed=True` 在哪一天改版后就彻底说不清了 —— 只能证明「曾经同意过什么」，
不能证明「同意的是不是当前这一版」。

**为什么要记 ip_hash**：留一个可核验的凭据证明这次同意确实来自该用户当时的环境，
同时用 HMAC 避免落明文 IP（见 core/net.py）。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, created_at_column


class ConsentRecord(Base):
    """一次「同意某版本文档」的记录"""

    __tablename__ = "consent_records"
    __table_args__ = (
        Index("ix_consent_records_user_id_document", "user_id", "document"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 文档标识：privacy | terms
    document: Mapped[str] = mapped_column(String(32), nullable=False)
    # 文档版本号（与前端静态页里标注的版本一致）
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ConsentRecord user={self.user_id} {self.document}@{self.version}>"
