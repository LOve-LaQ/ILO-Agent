# Main FastAPI Application Entry Point
"""
ILO-Agent Demo - FastAPI 主入口

运行方式:
    uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from loguru import logger
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import sys

# 添加项目根目录（backend/）到 Python 路径。
# 统一使用 `src.` 前缀导入，避免同一文件被 `core.` / `src.core.` 两条路径
# 加载成两个模块——否则 ILOException 等对象身份不一致，全局异常处理器会失配。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core.config import BACKEND_ROOT, settings, setup_logger
from src.core.errors import register_exception_handlers
from src.schemas.common import HealthCheckResponse, ServiceInfoResponse
from src.api.middleware import DeadlineMiddleware, RequestContextMiddleware
from src.api.routes.admin import router as admin_router
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


class SPAStaticFiles(StaticFiles):
    """前端静态托管 + SPA history 回退。

    两条路径不能被「回退到 index.html」吞掉，否则故障现场会极具误导性：

    - `api/` 开头的路径：不存在就必须 404（走全局错误契约）。若回退成 index.html，
      客户端拿到的是 200 + HTML，错误信息离真正的故障点十万八千里。
    - 带扩展名的资源（如 `assets/index-abc.js`）：文件不在就应当 404。回退 HTML 会让
      浏览器把 HTML 当 JS 解析，控制台报 `Unexpected token '<'`，排查成本极高。
    其余路径（`/learn/xxx` 这种前端路由）一律交给 index.html。
    """

    async def get_response(self, path: str, scope):
        # 归一化两个平台差异后再判断：mount 在 "/" 上时 path 可能带前导斜杠，
        # 且 Windows 下 Starlette 给的是 `api\v1\x` 这种反斜杠形式 ——
        # 直接用 `startswith("api/")` 判断会静默失配，API 的 404 被吞成 200 + HTML。
        clean = path.replace("\\", "/").strip("/")
        if clean == "api" or clean.startswith("api/"):
            raise StarletteHTTPException(status_code=404)
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or Path(clean).suffix:
                raise
            return await super().get_response("index.html", scope)


def _resolve_frontend_dist() -> Path | None:
    """定位前端构建产物；未开启托管或未构建时返回 None。

    路径基准固定为 `BACKEND_ROOT` 而非当前工作目录：`start_dev.py` 从项目根拉起，
    uvicorn 子进程 cwd 又在 `backend/`，用相对路径会指到两个不同目录。
    """
    if not settings.serve_frontend:
        return None

    raw = (settings.frontend_dist or "").strip()
    dist = Path(raw) if raw else BACKEND_ROOT.parent / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        logger.warning(
            f"[WARN] SERVE_FRONTEND 已开启但找不到构建产物（{dist / 'index.html'}），"
            "本次仅提供 API；请先执行 cd frontend && npm run build"
        )
        return None
    return dist


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
        resp = httpx.get(
            f"{settings.qdrant_url.rstrip('/')}/healthz",
            timeout=settings.qdrant_health_timeout,
        )
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

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        """采集任务的生命周期收尾 + 重依赖预热

        启动：把上一条进程留下的 running 批次标为 failed。进程被 kill（热重载、
        崩溃、容器重启）时后台任务没有机会执行 finish_batch，批次会永远停在
        running —— 运维看到一条「进行中」，实际那个任务早已不存在。
        关闭：取消还在跑的任务，让它们尽快落终态，而不是等下一次启动再兜。
        """
        from src.services import task_runner, warmup
        from src.services.collection_service import recover_orphan_batches

        await asyncio.to_thread(recover_orphan_batches)
        # 首页依赖在这里**同步**预热完（约 1s），而不是丢后台任务：
        # 丢后台的话，首个用户的请求会和预热抢同一个模块导入锁，同一份工作做两遍，
        # 实测把一次几十毫秒的请求拖到 4s。宁可启动晚 1s，也不要让用户等。
        # 失败只降级（首个请求慢一点），因此这里不抛。
        await warmup.run_feed_warmup()
        # 更贵的 LLM 栈（langchain → transformers → torch，实测 12s）会跟首个页面
        # 请求抢 GIL，改由首页把数据交给用户之后再预热 —— 见 services/warmup.py。
        try:
            yield
        finally:
            await task_runner.shutdown()

    app = FastAPI(
        title="ILO-Agent Demo",
        description="AI 技术情报官 · 智能技术资讯与学习 Agent",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
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
    # 请求级超时预算：写 request.state.deadline，供下游 clamp_timeout 取用。
    # 放在 request_id 之内 —— 预算只对真正会打下游的业务接口有意义。
    app.add_middleware(DeadlineMiddleware)
    
    # 注册路由
    # 权限边界：/discover 匿名可读；/learning 与 /me 必须登录（见 src/api/deps.py）
    app.include_router(discover_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(learning_router, prefix="/api/v1")
    app.include_router(me_router, prefix="/api/v1")
    # 管理后台：整个 router 由 require_admin 门禁保护（见 routes/admin.py）
    app.include_router(admin_router, prefix="/api/v1")

    # 全局异常处理：统一错误契约 {code, message, detail}
    register_exception_handlers(app)

    def _service_info() -> dict:
        return {
            "service": "ILO-Agent Demo",
            "version": "0.1.0",
            "status": "running",
            "docs": "/docs",
            "health": "/health",
        }

    # 单端口同源模式下，`/` 属于前端页面，服务信息挪到 /api/v1/info，
    # 否则打开域名看到一段 JSON —— 演示时第一印象就是「没部署好」。
    frontend_dist = _resolve_frontend_dist()
    if frontend_dist is None:
        @app.get("/", response_model=ServiceInfoResponse)
        async def root():
            return _service_info()
    else:
        @app.get(
            "/api/v1/info",
            response_model=ServiceInfoResponse,
            include_in_schema=False,
        )
        async def service_info():
            return _service_info()

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

    # 兜底挂载必须放在所有路由注册之后：Starlette 按注册顺序匹配，
    # mount("/") 一旦抢先就会把 /api、/docs、/health 全部当成静态路径。
    if frontend_dist is not None:
        app.mount(
            "/",
            SPAStaticFiles(directory=str(frontend_dist), html=True),
            name="frontend",
        )
        logger.info(
            f"[OK] 单端口同源模式：已托管前端 {frontend_dist}，"
            "对外只需暴露 8000（服务信息见 /api/v1/info）"
        )

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
