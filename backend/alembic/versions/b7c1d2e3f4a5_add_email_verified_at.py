"""add users.email_verified_at

阶段 5 补充：注册后发一封非阻塞的邮箱确认邮件
（见 src/services/email_verification_service.py）。

确认链接里的令牌是一枚 `typ=email_verify` 的 JWT，**不需要落库**（确认邮箱是
幂等状态，没有「用过即废」的需求），所以这次迁移只加一个时间戳列：
非空即代表该邮箱已确认可达。

Revision ID: b7c1d2e3f4a5
Revises: a1b2c3d4e5f6
Create Date: 2026-09-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7c1d2e3f4a5"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "email_verified_at")
