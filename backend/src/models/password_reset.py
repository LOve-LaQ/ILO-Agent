# Models - 密码重置令牌
"""password_reset_tokens 表（阶段 5 找回密码）。

**库里只存 token 的 sha256，不存明文**：reset token 是一次性凭证，谁拿到它就能
改掉这个账号的密码。明文入库意味着「库被拖走 = 所有待重置账号可被直接接管」。
哈希后即便库泄露，攻击者也拿不到可用的链接。

**为什么要单独一张表而不是塞进 Redis**：重置链接的有效期是 30 分钟，但「是否已用」
必须在重启 / Redis 清空后依然成立，否则一个已被用掉的 token 可能被重新接受。
真相源落库，Redis 只做可选的加速 —— 这里干脆不用 Redis。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, created_at_column


class PasswordResetToken(Base):
    """一次找回密码请求对应一行；用掉即置 used_at"""

    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # sha256 的 hex 固定 64 字符；唯一约束保证不会出现两个相同的令牌
    # （唯一索引自带，按 token_hash 精确查询无需再建单独索引）
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<PasswordResetToken user={self.user_id} used={self.used_at}>"
