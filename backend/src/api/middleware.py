# API - 请求上下文中间件
"""给每个请求注入 request_id（阶段 3 可观测性）。

同一个 id 同时进入：
- contextvars → 路由/服务里的 loguru 日志
- `user_activities.request_id` → 数据库里的行为流水
- 响应头 `x-request-id` → 前端报错时可以直接把 id 给出来

上游若已带 `x-request-id`（网关、前端重试链路）则沿用，便于跨服务串联同一条链路。
"""

import re

from typing import Optional

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from src.core.config import settings
from src.core.context import new_request_id, set_request_id
from src.core.deadline import new_deadline

REQUEST_ID_HEADER = "x-request-id"
MAX_REQUEST_ID_LEN = 64

# 白名单之外的字符一律剔除：上游/客户端可以随便伪造这个头，
# 若放行换行符就能往日志里插行（log forging），放行超长值则会让
# user_activities.request_id 插入失败 —— 而埋点异常是被吞掉的，流水会静默消失。
_UNSAFE_REQUEST_ID_CHARS = re.compile(r"[^A-Za-z0-9_.:-]")


def sanitize_request_id(raw: str | None) -> str:
    """沿用上游 request_id，但先做字符白名单与长度裁剪；不可用时自行生成"""
    if not raw:
        return new_request_id()
    cleaned = _UNSAFE_REQUEST_ID_CHARS.sub("", raw)[:MAX_REQUEST_ID_LEN]
    return cleaned or new_request_id()


class RequestContextMiddleware(BaseHTTPMiddleware):
    """把 request_id 放进 contextvars，并在响应头回显"""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        # call_next 会基于当前 context 创建下游任务，因此这里 set 的值对路由可见
        request_id = sanitize_request_id(request.headers.get(REQUEST_ID_HEADER))
        set_request_id(request_id)

        response = await call_next(request)

        response.headers[REQUEST_ID_HEADER] = request_id
        return response


# 请求级超时预算（秒）：路径正则 → Settings 字段名。
# 【为什么按路径匹配而不是按路由】BaseHTTPMiddleware 的 dispatch 早于路由解析，
# 此时 scope 里还没有 endpoint；路径是这一层唯一拿得到的信息。
# 未匹配到的接口不设 deadline（不限制）—— 预算只对真正会打下游的接口有意义。
_DEADLINE_RULES: tuple[tuple["re.Pattern", str], ...] = (
    (re.compile(r"^/api/v1/learning/chat/?$"), "deadline_learning_chat_seconds"),
    (re.compile(r"^/api/v1/learning/session/?$"), "deadline_learning_session_seconds"),
    (re.compile(r"^/api/v1/learning/response/?$"), "deadline_learning_response_seconds"),
    (re.compile(r"^/api/v1/discover/news/?$"), "deadline_discover_news_seconds"),
    (re.compile(r"^/api/v1/discover/cards/[^/]+/digest/?$"), "deadline_card_digest_seconds"),
)


def resolve_budget(path: str) -> Optional[float]:
    """按路径取该接口的请求级预算（秒）；无规则或配成非正数时返回 None（不限制）"""
    for pattern, setting_name in _DEADLINE_RULES:
        if pattern.match(path):
            value = getattr(settings, setting_name, 0.0)
            # 配 0 或负数表示「该接口不设预算」，便于排查时临时放行
            return value if value > 0 else None
    return None


class DeadlineMiddleware(BaseHTTPMiddleware):
    """给请求注入超时预算，写进 `request.state.deadline`。

    下游一律用 `clamp_timeout(本层配置超时, deadline)` 取本次调用真正可用的超时，
    返回 0 即表示预算已耗尽，应直接降级而不是发起调用。
    """

    async def dispatch(self, request: Request, call_next):
        budget = resolve_budget(request.url.path)
        if budget is not None:
            # Request.state 落在 scope["state"] 上，call_next 会把同一个 scope
            # 继续往下传，因此路由里能读到（不是中间件私有的副本）
            request.state.deadline = new_deadline(budget)
        return await call_next(request)


__all__ = [
    "MAX_REQUEST_ID_LEN",
    "REQUEST_ID_HEADER",
    "DeadlineMiddleware",
    "RequestContextMiddleware",
    "resolve_budget",
    "sanitize_request_id",
]
