# Card Schemas - 技术卡片统一契约
"""
技术卡片的统一字段模型（修掉 repo / article 两套命名）：
- 抓取层（github_fetcher / article_fetcher）产出的 payload 直接映射为 TechCard
- `_normalize_legacy` 兼容历史字段名：created_at -> published_at，updated_at -> source_updated_at
- source_url / source_platform / collected_at / batch_id / raw_description 为阶段 3 溯源字段，此处先占位
"""

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

# 受控分类词表，见 modules/discovery/summary_spec.py 的 CATEGORIES
CategoryCode = Literal[
    "backend",
    "frontend",
    "ai_ml",
    "devops",
    "database",
    "mobile",
    "security",
    "tools",
    "other",
]

CardType = Literal["repo", "article"]

_CATEGORY_SET = {
    "backend",
    "frontend",
    "ai_ml",
    "devops",
    "database",
    "mobile",
    "security",
    "tools",
    "other",
}


class TechCard(BaseModel):
    """统一技术卡片（仓库卡 / 文章卡共用）"""

    id: str
    repo_id: Optional[int] = None
    type: CardType = "repo"

    title: str
    summary: Optional[str] = None
    one_liner: Optional[str] = None
    problem: Optional[str] = None
    category: CategoryCode = "other"

    tags: List[str] = Field(default_factory=list)
    core_concepts: List[str] = Field(default_factory=list)
    tech_stack: List[str] = Field(default_factory=list)
    highlights: List[str] = Field(default_factory=list)
    use_cases: List[str] = Field(default_factory=list)

    link: Optional[str] = None
    source: Optional[str] = None
    # 阶段 3 溯源字段
    source_url: Optional[str] = None
    source_platform: Optional[str] = None

    # 仓库卡专属
    stars: Optional[int] = None
    language: Optional[str] = None
    # 文章卡专属
    score: Optional[int] = None
    comments: Optional[int] = None

    # 统一时间语义
    published_at: Optional[str] = Field(
        None, description="内容发布时间（repo 取 GitHub created_at；article 取平台发布时间）"
    )
    source_updated_at: Optional[str] = Field(
        None, description="源侧最后更新时间（repo 取 GitHub updated_at）"
    )

    # 阶段 3 溯源字段
    collected_at: Optional[str] = None
    batch_id: Optional[str] = None
    raw_description: Optional[str] = None

    # 个性化推送字段：仅 /discover/news 在「登录且有可用画像」时注入；
    # 匿名、新用户、回退随机时一律为 None —— 可选字段，既有卡片不受影响。
    recommend_score: Optional[float] = Field(
        None, ge=0, le=1, description="兴趣推荐分（0~1，越接近 1 越贴近画像）"
    )
    recommend_reason: Optional[str] = Field(
        None, description="推荐理由（一句话中文；非个性化时为 null）"
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy(cls, data: Any) -> Any:
        """兼容历史 payload：统一时间字段命名 + 收敛缺失/非法分类"""
        if not isinstance(data, dict):
            return data

        data = dict(data)

        # 统一时间字段命名（保留统一后的字段，丢弃旧名）
        if not data.get("published_at") and data.get("created_at"):
            data["published_at"] = data["created_at"]
        if not data.get("source_updated_at") and data.get("updated_at"):
            data["source_updated_at"] = data["updated_at"]
        data.pop("created_at", None)
        data.pop("updated_at", None)

        # 缺失 type 视为 repo（历史数据存在无 type 条目）
        if not data.get("type"):
            data["type"] = "repo"

        # 缺失/非法 category 收敛为 other，保证契约输出始终在受控词表内
        if data.get("category") not in _CATEGORY_SET:
            data["category"] = "other"

        return data
