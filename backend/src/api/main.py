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
    
    # CORS 中间件（允许前端跨域）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
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
    async def health_check():
        """健康检查接口"""
        return {
            "status": "healthy",
            "service": "ILO-Agent Demo"
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
