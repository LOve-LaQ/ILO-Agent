"""add user role and feature_flags

阶段 6：管理员可运行时关闭付费 LLM 模块。

两张改动：
1. `users.role` —— 角色分类。默认 'user'，提权只有「ADMIN_EMAILS 配置白名单」
   与「管理员在后台改」两条路，注册接口不接受客户端指定角色。
2. `feature_flags` —— 按 key 存全局开关。**不插入种子行**：代码里保留默认值，
   行不存在时按默认值处理，因此新部署无需预置数据，也不存在「迁移漏插某一行
   导致功能默认关闭」的隐患。

`users.role` 用 server_default 回填存量行：加 NOT NULL 列必须给出默认值，
否则已有账号会因缺列值而无法读取。

Revision ID: b6e0d4a9c731
Revises: f2a7c5b9d3e1
Create Date: 2026-11-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b6e0d4a9c731"
down_revision: Union[str, Sequence[str], None] = "f2a7c5b9d3e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column(
            "role",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'user'"),
        ),
    )

    op.create_table(
        "feature_flags",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "description",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_feature_flags_updated_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_feature_flags")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("feature_flags")
    op.drop_column("users", "role")
