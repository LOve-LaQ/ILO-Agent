# Main FastAPI Application Entry Point
"""
ILO-Agent Demo - FastAPI 主入口

运行方式:
    uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
import sys

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(__file__).rsplit("/", 1)[0] + "/../..")

from core.config import settings, setup_logger
from api.routes.discover import router as discover_router
from api.routes.learning import router as learning_router


# 初始化日志
setup_logger()


def create_application() -> FastAPI:
    """应用工厂函数"""
    
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
    
    # 注册路由
    app.include_router(discover_router, prefix="/api/v1")
    app.include_router(learning_router, prefix="/api/v1")
    
    @app.get("/")
    async def root():
        return {
            "service": "ILO-Agent Demo",
            "version": "0.1.0",
            "status": "running",
            "docs": "/docs",
            "health": "/health"
        }
    
    @app.get("/health")
    async def health_check():
        """健康检查接口"""
        return {
            "status": "healthy",
            "service": "ILO-Agent Demo"
        }
    
    # 自定义异常处理
    @app.exception_handler(Exception)
    async def global_exception_handler(request, exc):
        logger.error(f"Global exception: {exc}")
        raise HTTPException(
            status_code=500,
            detail=str(exc)
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
