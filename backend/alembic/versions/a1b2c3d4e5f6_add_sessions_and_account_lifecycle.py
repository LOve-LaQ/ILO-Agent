"""add sessions, password reset, email outbox, consent, account lifecycle

阶段 5 登录注册加固：新增 4 张表（user_sessions / password_reset_tokens /
email_outbox / consent_records）、users 的注销字段，并把 user_activities 里
历史明文 IP 回填为 HMAC 指纹（未配置 IP_HASH_SECRET 时清空，绝不保留明文）。

Revision ID: a1b2c3d4e5f6
Revises: 44de2b701220
Create Date: 2026-09-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "44de2b701220"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backfill_activity_ip() -> None:
    """把 user_activities.ip 的历史明文一次性转换为 HMAC 指纹。

    用 Python 侧逐行处理（而不是纯 SQL）：HMAC 的 secret 只存在于应用配置里，
    迁移里直接用同一个 hash_ip 函数才能保证与运行时口径一致。
    """
    from src.core.config import settings
    from src.core.net import hash_ip

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, ip FROM user_activities WHERE ip IS NOT NULL")
    ).fetchall()
    for row in rows:
        # 未配置 secret 时置空：既不能哈希，就绝不能继续保留明文
        new_value = hash_ip(row.ip) if settings.ip_hash_secret else None
        conn.execute(
            sa.text("UPDATE user_activities SET ip = :value WHERE id = :id"),
            {"value": new_value, "id": row.id},
        )


def upgrade() -> None:
    """Upgrade schema."""
    # ---------- users：账号注销字段 ----------
    op.add_column(
        "users",
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )

    # ---------- user_sessions ----------
    op.create_table(
        "user_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("refresh_jti", sa.String(length=64), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_jti", sa.String(length=64), nullable=True),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_sessions"),
        sa.UniqueConstraint("refresh_jti", name="uq_user_sessions_refresh_jti"),
    )
    op.create_index(
        "ix_user_sessions_user_id_revoked_at", "user_sessions", ["user_id", "revoked_at"]
    )

    # ---------- password_reset_tokens ----------
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_password_reset_tokens_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_password_reset_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_password_reset_tokens_token_hash"),
    )
    op.create_index(
        "ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"]
    )

    # ---------- email_outbox ----------
    op.create_table(
        "email_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_email", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_email_outbox"),
    )
    op.create_index("ix_email_outbox_to_email", "email_outbox", ["to_email"])
    op.create_index("ix_email_outbox_purpose", "email_outbox", ["purpose"])

    # ---------- consent_records ----------
    op.create_table(
        "consent_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_consent_records_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_consent_records"),
    )
    op.create_index(
        "ix_consent_records_user_id_document", "consent_records", ["user_id", "document"]
    )

    # ---------- 历史明文 IP 回填 ----------
    _backfill_activity_ip()


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_consent_records_user_id_document", table_name="consent_records")
    op.drop_table("consent_records")
    op.drop_index("ix_email_outbox_purpose", table_name="email_outbox")
    op.drop_index("ix_email_outbox_to_email", table_name="email_outbox")
    op.drop_table("email_outbox")
    op.drop_index("ix_password_reset_tokens_user_id", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_index("ix_user_sessions_user_id_revoked_at", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_column("users", "deleted_at")
    op.drop_column("users", "deletion_requested_at")
