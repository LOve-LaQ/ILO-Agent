# Models - 兴趣画像
"""用户兴趣画像（个性化推送）。

一行一个用户，`vector` 是「最近感兴趣的技术方向」的加权中心向量：

- **为什么存 JSONB 而不是 pgvector 列**：本项目的向量检索在 Qdrant，画像只在应用侧
  算一次余弦，不需要 ANN 索引；为一张表引入 pgvector 扩展与依赖不划算。
- **`embedding_model` 是语义空间指纹**：更换 embedding 提供方/模型后，维度与语义空间
  都会变；拿新模型的向量去比旧画像不是「降级」，而是**错配**（维度不同直接算不出，
  维度相同也会把不相关的方向算得很近）。因此指纹一变，画像必须作废重算。
- **`source_card_count`** 记录构建时用了多少张卡片，用于判断画像是否已经落后于
  用户的最新行为（数量对不上就重算）。
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, updated_at_column


class UserInterestProfile(Base):
    """用户兴趣向量（user_id 即主键，一人一行）"""

    __tablename__ = "user_interest_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # 加权平均后 L2 归一化的 float 列表（JSONB 存，长度 = 写入时的 VECTOR_SIZE）
    vector: Mapped[list] = mapped_column(JSONB, nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(64), nullable=False)
    source_card_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = updated_at_column()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<UserInterestProfile {self.user_id} cards={self.source_card_count}>"
