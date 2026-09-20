"""Alembic 运行环境。

关键约定：
1. 数据库 URL 唯一真相源是 backend/.env 的 DATABASE_URL（settings.database_url），
   alembic.ini 里留空，避免密码被复制成两份。
2. 与 backend/src/api/main.py 一致，统一用 `src.` 前缀导入。否则同一文件会以
   `models` / `src.models` 两条路径加载成两个模块，Base.metadata 出现两份，
   autogenerate 会误判成「需要删表再建表」。
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# backend/ 加入 sys.path，保证 `import src.*` 在任意工作目录下都成立
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.core.config import settings  # noqa: E402
from src.models import Base  # noqa: E402  （导入即触发全部模型注册到 Base.metadata）

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = (settings.database_url or "").strip()
    if not url:
        raise RuntimeError(
            "未配置 DATABASE_URL，Alembic 无法运行。"
            "请在 backend/.env 中设置（格式见 backend/.env.example）。"
        )
    return url


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连数据库"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连真实数据库执行迁移"""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
