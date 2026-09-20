# DB - SQLAlchemy 引擎与会话
"""
PostgreSQL 连接层（阶段 2 用户体系 / 阶段 3 可追溯链路）。

设计要点：
- 引擎**惰性创建**：未配置 DATABASE_URL 时不阻塞应用启动，未依赖数据库的
  接口（discover/news、契约测试）依旧可用；
- 真正需要连库时才抛出可操作的 RuntimeError，而不是静默连错库；
- get_db() 按请求产出 Session，用完即关。
"""

from typing import Iterator, Optional

from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import settings

_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None


def is_database_configured() -> bool:
    """DATABASE_URL 是否已配置（测试跳过、健康检查用）"""
    return bool(settings.database_url)


def mask_database_url(url: str) -> str:
    """打日志前抹掉密码"""
    if "@" not in url or "//" not in url:
        return url
    scheme, rest = url.split("//", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}//{user}:***@{host}"


def _build_engine() -> Engine:
    if not settings.database_url:
        raise RuntimeError(
            "未配置 DATABASE_URL，无法连接数据库。"
            "请在 backend/.env 中设置（格式见 backend/.env.example）。"
        )
    logger.info(f"Connecting to database: {mask_database_url(settings.database_url)}")
    return create_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


def get_engine() -> Engine:
    """获取（或首次创建）全局引擎"""
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False
        )
    return _session_factory


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个 Session"""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def get_optional_db() -> Iterator[Optional[Session]]:
    """FastAPI 依赖：未配置 DATABASE_URL 时产出 None

    给「登录可选」的公开接口（/discover/*）用：这些接口在无数据库的纯 demo
    环境下必须照常可用，不能因为拿不到会话而 500。
    """
    if not is_database_configured():
        yield None
        return
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def reset_engine() -> None:
    """测试用：丢弃当前引擎（切换 DATABASE_URL 后重新建连）"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
