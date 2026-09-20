# API - 请求上下文中间件
"""给每个请求注入 request_id（阶段 3 可观测性）。

同一个 id 同时进入：
- contextvars → 路由/服务里的 loguru 日志
- `user_activities.request_id` → 数据库里的行为流水
- 响应头 `x-request-id` → 前端报错时可以直接把 id 给出来

上游若已带 `x-request-id`（网关、前端重试链路）则沿用，便于跨服务串联同一条链路。
"""

import re

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from src.core.context import new_request_id, set_request_id

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


__all__ = [
    "MAX_REQUEST_ID_LEN",
    "REQUEST_ID_HEADER",
    "RequestContextMiddleware",
    "sanitize_request_id",
]
