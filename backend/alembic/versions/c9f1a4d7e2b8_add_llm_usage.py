"""add llm_usage ledger

阶段 7：LLM 成本可观测与预测。

一张新表 `llm_usage`，一次真实调用一行：
- token 与耗时：来自上游 `usage` 字段，缺失时按字符估算并置 `tokens_estimated`；
- 金额 `cost_cny`：**写入时**按当时单价算好（价目表见 `core/pricing.py`）。
  不在查询时用「当前单价 × 历史 token」，否则一次调价会把过去的账全部改写；
- `success`：失败行金额为 0，但保留下来当失败率的分母。

索引只建 `created_at` 与 `purpose`：面板的主查询是「最近 N 天全量拉取后在 Python 聚合」，
明细量级在万行以内，不需要更多复合索引。

`user_id` 用 ON DELETE SET NULL：删账号不该连带删掉这段成本史（账单要可审计）。

Revision ID: c9f1a4d7e2b8
Revises: b6e0d4a9c731
Create Date: 2026-11-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c9f1a4d7e2b8"
down_revision: Union[str, Sequence[str], None] = "b6e0d4a9c731"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_cny", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("tokens_estimated", sa.Boolean(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_llm_usage_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_usage")),
    )
    op.create_index(
        "ix_llm_usage_created_at", "llm_usage", ["created_at"], unique=False
    )
    op.create_index("ix_llm_usage_purpose", "llm_usage", ["purpose"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_llm_usage_purpose", table_name="llm_usage")
    op.drop_index("ix_llm_usage_created_at", table_name="llm_usage")
    op.drop_table("llm_usage")
