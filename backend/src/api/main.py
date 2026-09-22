# Main FastAPI Application Entry Point
"""
ILO-Agent Demo - FastAPI 主入口

运行方式:
    uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pathlib import Path
import sys

# 添加项目根目录（backend/）到 Python 路径。
# 统一使用 `src.` 前缀导入，避免同一文件被 `core.` / `src.core.` 两条路径
# 加载成两个模块——否则 ILOException 等对象身份不一致，全局异常处理器会失配。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core.config import settings, setup_logger
from src.core.errors import register_exception_handlers
from src.schemas.common import HealthCheckResponse, ServiceInfoResponse
from src.api.middleware import RequestContextMiddleware
from src.api.routes.auth import router as auth_router
from src.api.routes.discover import router as discover_router
from src.api.routes.learning import router as learning_router
from src.api.routes.me import router as me_router
from src.services.captcha_service import assert_security_config


# 初始化日志
setup_logger()


def _cors_origins() -> list[str]:
    """解析 CORS 白名单。

    绝不使用 `["*"]`：`allow_origins=["*"]` 与 `allow_credentials=True` 同时开启是
    明确的纵深防御破口（refresh token 走 Cookie，必须限定来源）。留空时回退到
    `frontend_base_url`，保证「前后端分离直连」这一开发姿势不被误伤；
    开发期前端走 vite proxy 同源，本就不需要跨域。
    """
    raw = (settings.cors_allow_origins or "").strip()
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    fallback = (settings.frontend_base_url or "").strip().rstrip("/")
    return [fallback] if fallback else []


def _probe_dependencies() -> dict:
    """探测关键依赖可用性（全部 best-effort，异常即记为不可用）。

    这些依赖任一不可用都会造成业务降级：Qdrant 挂掉首页会退化成内置示例、
    Redis 挂掉限流与会话撤销失效、PostgreSQL 挂掉「我的」与溯源整体不可用。
    健康检查必须如实反映，否则监控形同虚设 —— 此前硬编码 healthy，
    Qdrant 静默停摆时探测仍报健康，首页只剩 3 条却查不出原因。
    """
    checks: dict = {}

    try:
        from src.core.redis_client import get_redis

        checks["redis"] = "ok" if get_redis() is not None else "unavailable"
    except Exception as e:  # noqa: BLE001 - 探测失败不能把健康检查自己打倒
        logger.warning(f"[WARN] 健康检查探测 Redis 失败: {e}")
        checks["redis"] = "error"

    try:
        from src.core.db import get_engine, is_database_configured

        if not is_database_configured():
            # 未配置 DATABASE_URL 是纯 demo 环境的合法状态，不算故障
            checks["postgres"] = "not_configured"
        else:
            from sqlalchemy import text

            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 健康检查探测 PostgreSQL 失败: {e}")
        checks["postgres"] = "error"

    try:
        import httpx

        # 直接打 Qdrant 的 /healthz，避免拉起知识库单例（那会连带初始化 collection）
        resp = httpx.get(f"{settings.qdrant_url.rstrip('/')}/healthz", timeout=2.0)
        checks["qdrant"] = "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 健康检查探测 Qdrant 失败: {e}")
        checks["qdrant"] = "error"

    return checks


def create_application() -> FastAPI:
    """应用工厂函数"""

    # 启动期安全自检：生产环境把「人机校验未配置」「邮件用 console」这类
    # 「忘了配 = 没防护」的错配在启动时就挡下，而不是等第一次注册才暴露。
    assert_security_config()

    app = FastAPI(
        title="ILO-Agent Demo",
        description="AI 技术情报官 · 智能技术资讯与学习 Agent",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc"
    )

    # CORS 中间件：来源白名单（见 _cors_origins）。白名单为空时不注册该中间件 ——
    # 同源部署（vite proxy）下没有跨域请求，注册一个空名单只会徒增混淆。
    origins = _cors_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    else:
        logger.warning(
            "[WARN] CORS 白名单为空（FRONTEND_BASE_URL 与 CORS_ALLOW_ORIGINS 均未配），"
            "未注册跨域中间件；如需前后端跨域直连请配置其一。"
        )

    # 后加的在更外层：request_id 需要覆盖所有请求（含 CORS 预检失败的情况）
    app.add_middleware(RequestContextMiddleware)
    
    # 注册路由
    # 权限边界：/discover 匿名可读；/learning 与 /me 必须登录（见 src/api/deps.py）
    app.include_router(discover_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(learning_router, prefix="/api/v1")
    app.include_router(me_router, prefix="/api/v1")

    # 全局异常处理：统一错误契约 {code, message, detail}
    register_exception_handlers(app)

    @app.get("/", response_model=ServiceInfoResponse)
    async def root():
        return {
            "service": "ILO-Agent Demo",
            "version": "0.1.0",
            "status": "running",
            "docs": "/docs",
            "health": "/health"
        }
    
    @app.get("/health", response_model=HealthCheckResponse)
    def health_check():
        """健康检查接口（真实探测依赖，而不是硬编码 healthy）

        声明为同步函数：FastAPI 会把它丢进线程池执行，避免阻塞事件循环 ——
        探测里的 Redis / PostgreSQL / Qdrant 都是阻塞 IO。
        判定口径：任一依赖不可用即 `degraded`（未配置数据库视为「纯 demo 环境」，不算故障）。
        """
        checks = _probe_dependencies()
        healthy = all(value in ("ok", "not_configured") for value in checks.values())
        return {
            "status": "healthy" if healthy else "degraded",
            "service": "ILO-Agent Demo",
            "checks": checks,
        }
    
    return app


# 应用实例
app = create_application()


if __name__ == "__main__":
    import uvicorn
    
    logger.info("🚀 Starting ILO-Agent Demo API server...")
    uvicorn.run(
        "src.api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
