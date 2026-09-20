# Models - 用户
"""users 表（阶段 2 权限边界）。

用户身份的唯一真相源。此前 learning/* 用请求体里写死的 `demo-user`，
现在一律从 access token 的 `sub`（即本表主键）解析。
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from src.models.base import Base, created_at_column, updated_at_column


class User(Base):
    """注册用户"""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    username: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, index=True
    )
    # bcrypt 哈希串固定 60 字符，留出余量以兼容未来更换算法
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=expression.true()
    )
    # 邮箱确认（阶段 5）：用户点击确认邮件里的链接后写入。
    # 注册不做阻塞式邮箱校验（用户若填错邮箱，仍能正常使用），因此这里允许为空，
    # 它只承担「让用户自己发现邮箱填错了」的提示作用。
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 账号注销（阶段 5）：申请注销的时间。冷静期内 is_active 置 false、可撤销；
    # 为什么不做「申请即删」：误操作与账号被盗后的冲动注销都不可逆，留一个窗口更稳。
    deletion_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 冷静期到期执行匿名化时写入。非空即代表「这是已注销的匿名壳」，
    # 用于区分「正常账号」与「已释放邮箱的占位行」。
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<User {self.username} ({self.email})>"
