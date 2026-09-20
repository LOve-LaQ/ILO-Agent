# Core - 请求级上下文
"""请求级上下文变量（阶段 3 可观测性）。

单独成模块是为了打破循环依赖：`core/config.py`（日志格式）、`services/activity_service.py`
（埋点写库）、`api/middleware.py`（中间件注入）都要读同一个 request_id，
而 config 被 core.db 依赖、core.db 又被 services 依赖 —— 放在这里谁都可以安全引用。
"""

import uuid
from contextvars import ContextVar
from typing import Optional

_request_id: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    """生成一个请求链路 ID"""
    return uuid.uuid4().hex


def set_request_id(request_id: str) -> None:
    """由中间件在请求入口写入"""
    _request_id.set(request_id)


def get_request_id() -> Optional[str]:
    """读取当前请求的链路 ID（日志与埋点共用）"""
    return _request_id.get()
