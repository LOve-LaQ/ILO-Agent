"""add collection_records.raw_content / content_meta

「先读原文」需要把仓库 README 作为**内容真相源**留存下来：
- `raw_content` 存原文快照本身
- `content_meta` 存 path / sha / size / source / fetched_at，回答「读的是哪个版本」

两列都可空，且**不做数据回填**：存量卡片没有快照，由读取端点首次打开时按需补抓一次
（见 `collection_service.get_or_fetch_card_content`），避免在迁移里发起外部网络请求。

Revision ID: c3f8a91b7d24
Revises: b7c1d2e3f4a5
Create Date: 2026-09-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c3f8a91b7d24"
down_revision: Union[str, Sequence[str], None] = "b7c1d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "collection_records",
        sa.Column("raw_content", sa.Text(), nullable=True),
    )
    op.add_column(
        "collection_records",
        sa.Column("content_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("collection_records", "content_meta")
    op.drop_column("collection_records", "raw_content")
