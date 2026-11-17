# Admin Schemas - 管理后台契约
"""阶段 6：功能开关与用户角色的对外契约。

【角色为什么用 str 而不是 Literal["user","admin"]】合法取值集合的唯一真相源是
`core/roles.py`。在 schema 里再写一遍 Literal，等于把同一份名单维护两次：
将来加 operator 时改了 roles.py 却忘了这里，接口会在校验层 422，
而且错误信息是 pydantic 的原始文案，不是我们想给用户看的那句。
放在路由里用 VALID_ROLES 校验，能返回带 code 的统一错误信封。
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class FeatureFlagItem(BaseModel):
    """一个功能开关的当前状态"""

    key: str = Field(..., description="开关标识，如 learning_chat")
    enabled: bool = Field(..., description="当前是否开启（数据库值为准）")
    default_enabled: bool = Field(..., description="代码里的默认值（用于一键恢复）")
    description: str = Field("", description="这个开关控制什么")
    updated_at: Optional[datetime] = Field(None, description="最近一次修改时间")
    updated_by: Optional[str] = Field(None, description="最近一次修改人的用户 id")


class FeatureFlagListResponse(BaseModel):
    """GET /admin/features 响应"""

    items: List[FeatureFlagItem]
    count: int


class FeatureFlagUpdateRequest(BaseModel):
    """PATCH /admin/features/{key} 请求体"""

    enabled: bool = Field(..., description="目标状态：true 开启 / false 关闭")


class AdminUserItem(BaseModel):
    """后台用户列表里的一行（不暴露 password_hash 等敏感列）"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    username: str
    role: str = Field(..., description="user | admin（已合并 ADMIN_EMAILS 白名单）")
    is_active: bool
    created_at: datetime


class AdminUserListResponse(BaseModel):
    """GET /admin/users 响应"""

    items: List[AdminUserItem]
    count: int


class AdminUserRoleUpdateRequest(BaseModel):
    """PATCH /admin/users/{user_id}/role 请求体"""

    role: str = Field(..., description="目标角色：user | admin")


# ==================== LLM 成本（阶段 7）====================


class LlmCostTotals(BaseModel):
    """所选窗口内的用量与花费总额"""

    calls: int = Field(..., description="窗口内调用次数（含失败）")
    success_calls: int = Field(..., description="成功次数")
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_cny: float = Field(..., description="成功调用产生的费用合计（元）")
    avg_latency_ms: int = Field(0, description="成功调用的平均耗时（毫秒）")
    success_rate: float = Field(0.0, description="成功率 0~1")
    estimated_calls: int = Field(0, description="token 由字符估算（上游未返回 usage）的调用数")


class DailyCostPoint(BaseModel):
    """日成本曲线上的一个点（窗口内空缺日为 0，保证曲线连续）"""

    date: str = Field(..., description="本地日期 YYYY-MM-DD（UTC+8）")
    calls: int
    tokens: int
    cost_cny: float


class PurposeCostItem(BaseModel):
    """按业务用途拆分的成本"""

    purpose: str
    label: str = Field(..., description="用途的中文名，可直接展示")
    calls: int
    tokens: int
    cost_cny: float
    avg_cost_cny: float = Field(0.0, description="单次平均花费，用于横向比较成本量级")
    share: float = Field(0.0, description="占窗口总花费的比例 0~1")


class ModelCostItem(BaseModel):
    """按模型拆分的成本"""

    model: str
    provider: str
    calls: int
    cost_cny: float
    price_known: bool = Field(..., description="是否命中本地价目表；false 表示金额为兜底价估算")


class CostForecast(BaseModel):
    """成本预测（最小二乘线性回归）"""

    method: str = Field(..., description="predict 方法标识，当前为 least_squares")
    samples: int = Field(..., description="参与拟合的天数")
    slope_cny_per_day: float = Field(..., description="日成本的变化速度（元/天），正数为上升")
    r2: float = Field(..., description="拟合优度 0~1，越低说明趋势越不可信")
    confidence: str = Field(..., description="high | medium | low | insufficient")
    next_7d_cny: float = Field(..., description="未来 7 天预测花费（元）")
    daily_avg_cny: float = Field(..., description="预测期内日均花费（元）")
    daily_forecast: List[float] = Field(default_factory=list, description="逐日预测值")
    mtd_cny: float = Field(0.0, description="本月至今已花（元）")
    month_remaining_days: int = Field(0, description="本月剩余天数（不含今天）")
    month_projection_cny: float = Field(0.0, description="本月预测总额 = 已花 + 日均 × 剩余天数")
    budget_cny: float = Field(0.0, description="月度预算；0 表示未设预算")
    budget_used_ratio: Optional[float] = Field(None, description="预算使用比例；未设预算时为 null")
    days_until_budget: Optional[float] = Field(
        None, description="按当前速度，预算还能撑多少天；未设预算或无花费时为 null"
    )


class LlmUsageResponse(BaseModel):
    """GET /admin/llm/usage 响应：成本面板所需的全部数据"""

    window_days: int
    timezone: str = Field(..., description="分桶时区，固定 Asia/Shanghai")
    generated_at: datetime
    currency: str = Field("CNY", description="金额单位，当前只有人民币")
    totals: LlmCostTotals
    series: List[DailyCostPoint]
    by_purpose: List[PurposeCostItem]
    by_model: List[ModelCostItem]
    forecast: CostForecast
    notes: List[str] = Field(default_factory=list, description="后端生成的结论句，直接展示")
    price_table_note: str = Field("", description="计价口径说明（金额是写入时算的）")
