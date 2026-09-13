# Core Configuration Module
"""
ILO-Agent Demo 核心配置
- 环境变量管理
- 日志系统初始化
"""

import os
from pydantic_settings import BaseSettings
from loguru import logger


class Settings(BaseSettings):
    """应用配置项"""
    
    # API Keys
    # Optional - use DEEPSEEK_API_KEY in state_machine instead
    openai_api_key: str = ""
    redis_url: str = "redis://127.0.0.1:6379/0"
    qdrant_url: str = "http://127.0.0.1:6333"
    mem0_api_key: str = ""
    
    # Application
    log_level: str = "INFO"
    environment: str = "development"
    
    # Agent Configuration
    max_tokens: int = 4096
    num_questions: int = 3
    
    # Rate Limiting
    request_rate_limit: int = 100  # requests per minute
    
    class Config:
        env_file = ".env"
        extra = "ignore"


# 全局配置实例
settings = Settings()


def setup_logger():
    """初始化日志系统"""
    logger.remove()  # 移除默认处理器
    
    logger.add(
        "logs/app_{time:YYYY-MM-DD_HH-mm-SS}.log",
        rotation="10 MB",
        retention="7 days",
        level=settings.log_level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{function}:{line} - {message}"
    )
    
    logger.info(f"🚀 ILO-Agent Demo started in {settings.environment} mode")


__all__ = ["settings", "setup_logger"]
