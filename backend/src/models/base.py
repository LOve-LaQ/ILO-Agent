# Models - 声明式基类
"""SQLAlchemy 2.0 声明式基类与公共列工具。"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 约束/索引命名约定：让 Alembic 生成与后续变更都能拿到确定的名字。
# 必须在首次迁移之前定下——之后再改就需要额外迁移来重命名。
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的基类（Alembic target_metadata 指向它）"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """带时区的当前时间（库中统一存 timestamptz）"""
    return datetime.now(timezone.utc)


def created_at_column() -> Mapped[datetime]:
    """created_at 列：插入时由数据库填充"""
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def updated_at_column() -> Mapped[datetime]:
    """updated_at 列：插入/更新时由数据库填充"""
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
