# Models - 功能开关
"""feature_flags 表：管理员可在**不重启**的前提下关闭付费 LLM 模块。

【为什么落库，而不是只放 .env】.env 开关只能重启生效。而「演示前关掉抓取、
演示时再打开」这种操作恰恰要在运行中完成；重启一次后端要等 10~20s（LLM 客户端
初始化），演示现场根本等不起。

【为什么 key 直接用主键、不建自增 id】开关的语义就是「按名字查一行」，
天然以 key 为标识；再加一层无意义的代理主键只会让 upsert 多一次唯一索引维护。

【为什么没有「按用户灰度」字段】当前只有一个管理维度：全局开/关。
加白名单灰度会把一次主键查询变成 join + 规则求值，收益不成比例。
真需要灰度时再扩表，迁移成本很低。
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class FeatureFlag(Base):
    """一个可运行时切换的功能开关"""

    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    description: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    # 记录「谁改的」：开关直接影响付费成本，出问题时必须能追溯到操作人。
    # SET NULL 而非 CASCADE：删用户不该连带删掉这段操作史。
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<FeatureFlag {self.key}={self.enabled}>"
