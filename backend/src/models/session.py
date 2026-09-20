# Models - 登录会话
"""user_sessions 表（阶段 5 会话可撤销）。

**为什么要给「无状态」的 JWT 配一张会话表**：JWT 的优势是无状态、不查库，
代价是**无法撤销** —— 用户点了「退出所有设备」、改了密码、或怀疑账号被盗，
服务端手里没有任何开关能立刻让旧凭证失效，只能等它自己过期（最长 14 天）。
这张表就是那个开关：每张 refresh token 对应一行，撤销一行即撤销一个设备。

**为什么 refresh 要轮换**：refresh 有效期内每次刷新都换发新 token 并作废旧 token，
这样「同一个旧 token 被用第二次」就成了可检测的盗用信号（reuse detection）。
若从不轮换，被盗的 refresh 能在 14 天里与正常用户并存，服务端毫无察觉。

**为什么记 jti 而不是整串 token**：jti 是 token 载荷里的随机 id，够唯一、够短，
且不像整串 token 那样一旦落库就等于把凭证本身存进了数据库。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, created_at_column


class UserSession(Base):
    """一个登录会话（≈ 一台设备上的一次登录）"""

    __tablename__ = "user_sessions"
    # 会话列表与「撤销某用户全部会话」的主查询都是「按 user_id 过滤 + 看是否已撤销」，
    # 建复合索引刚好覆盖这两个条件
    __table_args__ = (
        Index("ix_user_sessions_user_id_revoked_at", "user_id", "revoked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 当前有效的 refresh token 的 jti；轮换时旧行保留（用于 reuse 判定），
    # 因此这里必须唯一，否则无法「按 jti 精确查到是哪一行被重复使用」。
    # 唯一约束在 PG 里自带唯一索引，按 jti 的查询无需再单独建索引。
    refresh_jti: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 轮换链：旧行指向新行的 jti，reuse 时据此判断「新会话是否仍活跃」
    replaced_by_jti: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 设备信息尽可能避免明文 PII：IP 走 HMAC（见 core/net.py），UA 是技术标识可保留
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<UserSession {self.id} user={self.user_id} revoked={self.revoked_at}>"
