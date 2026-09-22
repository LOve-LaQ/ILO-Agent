"""add collection_records.content_digest_zh / content_digest_meta

大量仓库 README 只有英文，中文用户点开「先读原文」等于读不懂。这两列存一份
**按需生成、长期复用**的中文导读：

- `content_digest_zh` 存导读正文（Markdown）
- `content_digest_meta` 存 `{source_fingerprint, source_chars, generated_at}`，
  用 `source_fingerprint` 绑定生成时的那份原文；原文一改指纹就变，导读自动失效

两列都可空，且**不做数据回填**：导读是按需生成的付费 LLM 调用，不该在迁移里
批量触发（那样等于给所有历史卡片一次性买单）。缺失时由读取端点首次请求时生成
（见 `collection_service.get_or_generate_content_digest`）。

Revision ID: d9a4e7b2f1c8
Revises: c3f8a91b7d24
Create Date: 2026-09-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d9a4e7b2f1c8"
down_revision: Union[str, Sequence[str], None] = "c3f8a91b7d24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "collection_records",
        sa.Column("content_digest_zh", sa.Text(), nullable=True),
    )
    op.add_column(
        "collection_records",
        sa.Column(
            "content_digest_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("collection_records", "content_digest_meta")
    op.drop_column("collection_records", "content_digest_zh")
