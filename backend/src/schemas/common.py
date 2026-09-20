# Common Schemas - 通用响应模型
"""
跨模块复用的基础响应契约：
- ErrorResponse: 统一错误结构 {code, message, detail}（所有非 2xx 均返回此结构）
- PageMeta: 列表分页元信息
"""

from typing import Any, Optional

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """统一错误响应契约"""

    code: str = Field(..., description="业务错误码，如 SESSION_NOT_FOUND")
    message: str = Field(..., description="面向用户的中文错误信息")
    detail: Optional[Any] = Field(None, description="调试细节（生产环境可为空）")


class PageMeta(BaseModel):
    """列表分页元信息"""

    count: int = Field(..., description="本次返回条数")
    total: int = Field(..., description="总量")
    has_more: bool = Field(..., description="是否还有更多")


class ServiceInfoResponse(BaseModel):
    """GET / 服务自述"""

    service: str = Field(..., description="服务名")
    version: str = Field(..., description="版本号")
    status: str = Field(..., description="运行状态")
    docs: str = Field(..., description="Swagger 文档路径")
    health: str = Field(..., description="健康检查路径")


class HealthCheckResponse(BaseModel):
    """GET /health 健康检查"""

    status: str = Field(..., description="健康状态：healthy | degraded")
    service: str = Field(..., description="服务名")
