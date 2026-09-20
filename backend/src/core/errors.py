# Errors - 统一业务异常与错误契约
"""
统一错误契约 {code, message, detail}：
- ILOException: 业务异常，路由内主动抛出
- register_exception_handlers: 把 ILOException / HTTPException / 校验错误 / 未捕获异常
  全部归一成同一 ErrorResponse 结构，避免前端再面对多种错误格式
"""

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.schemas.common import ErrorResponse


class ILOException(Exception):
    """业务异常基类（统一错误契约）"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        detail=None,
        headers: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail
        # 少数错误必须带响应头才有意义：429 的 `Retry-After` 告诉客户端还要等多久，
        # 没有它前端只能干等或盲目重试，反而加重限流压力。
        self.headers = headers


# 路由级默认错误响应：让 ErrorResponse 进入 OpenAPI，前端才能从 schema 派生错误类型，
# 同时覆盖 FastAPI 自动生成的 422 HTTPValidationError（实际返回的是本项目的错误信封）。
ERROR_RESPONSES: dict[int | str, dict] = {
    400: {"model": ErrorResponse, "description": "业务错误（{code, message, detail}）"},
    401: {"model": ErrorResponse, "description": "未登录 / 凭证无效或已过期"},
    403: {"model": ErrorResponse, "description": "已登录但无权访问"},
    404: {"model": ErrorResponse, "description": "资源不存在"},
    409: {"model": ErrorResponse, "description": "状态冲突"},
    422: {"model": ErrorResponse, "description": "参数校验失败"},
    429: {"model": ErrorResponse, "description": "请求过于频繁（限流，响应头带 Retry-After）"},
    500: {"model": ErrorResponse, "description": "服务内部错误"},
    502: {"model": ErrorResponse, "description": "上游依赖失败"},
}


def _error_payload(code: str, message: str, detail=None) -> dict:
    return {"code": code, "message": message, "detail": detail}


def _first_validation_message(exc: RequestValidationError) -> str:
    """取第一条校验错误作为 message。

    默认 FastAPI 只给「请求参数校验失败」，前端 `cause.message` 直接展示给用户时
    等于什么都没说。这里取第一条错误的原文（并剥掉 pydantic 给自定义 ValueError
    加上的 `Value error, ` 前缀），让「密码不能包含连续 4 位以上的顺序字符」这类
    具体原因能直达用户。完整错误列表仍保留在 detail 里，机器可读性不受影响。
    """
    for error in exc.errors():
        message = str(error.get("msg") or "").strip()
        if not message:
            continue
        prefix = "Value error, "
        if message.startswith(prefix):
            message = message[len(prefix):]
        return message
    return "请求参数校验失败"


def register_exception_handlers(app: FastAPI) -> None:
    """在 app 上注册全局异常处理器"""

    @app.exception_handler(ILOException)
    async def _ilo_exception_handler(request: Request, exc: ILOException):
        logger.warning(f"[{exc.code}] {exc.message}")
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(exc.code, exc.message, jsonable_encoder(exc.detail)),
            headers=exc.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        # 把 HTTPException.detail 归一为 message
        message = exc.detail if isinstance(exc.detail, str) else "请求失败"
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(f"HTTP_{exc.status_code}", message, jsonable_encoder(exc.detail)),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=_error_payload(
                "VALIDATION_ERROR",
                _first_validation_message(exc),
                jsonable_encoder(exc.errors()),
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception(f"Unhandled exception: {exc}")
        return JSONResponse(
            status_code=500,
            content=_error_payload("INTERNAL_ERROR", "服务器内部错误，请稍后重试", str(exc)),
        )
