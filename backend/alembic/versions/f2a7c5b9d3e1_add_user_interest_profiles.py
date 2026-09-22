"""add user_interest_profiles

个性化推送的画像表：一人一行，存「用户最近感兴趣的技术方向」的加权中心向量。

- `vector` 用 JSONB 存 float 列表，而不是 pgvector 列：向量检索在 Qdrant，画像只在
  应用侧算一次余弦，不需要 ANN 索引，为一张表引入 pgvector 扩展不划算；
- `embedding_model` 是**语义空间指纹**：换模型后维度与语义空间都会变，旧画像必须
  作废重算，否则是拿新向量错配旧画像；
- `source_card_count` 记录构建时用了几张卡片，用于判断画像是否需要重算。

按需创建、**不做数据回填**：画像由首次请求画像的登录用户自己的行为触发，
没有历史数据可回填（回填等于替所有用户凭空造兴趣）。

Revision ID: f2a7c5b9d3e1
Revises: d9a4e7b2f1c8
Create Date: 2026-09-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f2a7c5b9d3e1"
down_revision: Union[str, Sequence[str], None] = "d9a4e7b2f1c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "user_interest_profiles",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("vector", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("embedding_model", sa.String(length=64), nullable=False),
        sa.Column("source_card_count", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_interest_profiles_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_user_interest_profiles")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("user_interest_profiles")
