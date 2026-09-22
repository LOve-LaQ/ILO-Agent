# Discover Schemas - 资讯发现接口契约
"""
- CardListResponse: GET /discover/news 与 /discover/articles 的统一响应
- RefreshResponse: POST /discover/refresh 与 /discover/refresh-articles 的响应
- ProvenanceResponse: GET /discover/cards/{card_id}/provenance 的内容溯源响应
- CardContentResponse: GET /discover/cards/{card_id}/content 的原文快照响应
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .card import TechCard


class CardListResponse(BaseModel):
    """卡片列表统一响应"""

    items: List[TechCard]
    count: int
    total: int
    has_more: bool
    source: str = Field(..., description="数据来源：knowledge_base | github | sample")


class RefreshResponse(BaseModel):
    """抓取结果响应（各降级分支字段可选，故均给默认值）"""

    status: str = Field(..., description="ok | empty")
    new_count: int = 0
    skipped_count: int = 0
    total_in_kb: int = 0
    count: int = 0
    message: Optional[str] = None
    fallback: Optional[str] = None
    batch_id: Optional[str] = Field(
        None, description="本次采集批次号（collection_batches.id），用于内容溯源"
    )


class TrendingResponse(BaseModel):
    """GET /discover/trending 响应"""

    items: List[TechCard]
    updated_at: str


class TaggedNewsResponse(BaseModel):
    """GET /discover/by-tag/{tag} 响应"""

    tag: str
    items: List[TechCard]
    count: int


# ==================== 内容溯源（阶段 3） ====================


class ProvenanceBatch(BaseModel):
    """采集批次（一次抓取动作）"""

    id: str
    kind: str = Field(..., description="repo | article")
    trigger: str = Field(..., description="manual | scheduled")
    status: str = Field(..., description="running | succeeded | partial | failed")
    params: Optional[Dict[str, Any]] = Field(None, description="本次抓取参数，用于复现")
    fetched_count: int = 0
    new_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class ProvenanceResponse(BaseModel):
    """GET /discover/cards/{card_id}/provenance 响应（匿名可读）

    回答「这张卡片是从哪来的」：哪次采集、哪个链接、摘要生成前的原文是什么。
    """

    item_id: str
    known: bool = Field(
        ..., description="是否有采集溯源记录；false 多为历史卡片或内置示例数据"
    )
    source_platform: Optional[str] = Field(
        None, description="github | hackernews | lobsters | devto | stackoverflow"
    )
    source_url: Optional[str] = None
    raw_description: Optional[str] = Field(
        None, description="摘要生成前的原文；摘要失败重试与人工核对都依赖它"
    )
    collected_at: Optional[datetime] = None
    summary_generated_at: Optional[datetime] = None
    status: Optional[str] = Field(None, description="summarized | deduped | failed")
    backfilled: bool = Field(False, description="是否为历史数据回填（时间与原文为近似值）")

    batch: Optional[ProvenanceBatch] = None
    snapshot: Optional[TechCard] = Field(None, description="入库当时的卡片快照")
    card: Optional[TechCard] = Field(None, description="当前知识库/缓存里的卡片（可能已被覆盖）")


# ==================== 原文快照（「先读原文」） ====================


class CardContentMeta(BaseModel):
    """原文快照的版本信息（回答「读的是哪个版本」）"""

    path: Optional[str] = Field(None, description="README 文件名，如 README.md")
    sha: Optional[str] = Field(
        None, description="git blob sha；raw CDN 兜底路径拿不到时为 null"
    )
    size: Optional[int] = Field(None, description="原始字节数（截断前）")
    source: Optional[str] = Field(None, description="github-api | raw-cdn")
    truncated: bool = Field(False, description="是否因超出字符上限而截断")
    fetched_at: Optional[str] = Field(None, description="获取时间（ISO 8601）")


class CardContentResponse(BaseModel):
    """GET /discover/cards/{card_id}/content 响应（匿名可读）

    `origin` 决定前端展示方式：
    - snapshot：本地已有快照，直接渲染 content
    - on_demand：本次实时补抓并已回写，后续请求会变成 snapshot
    - unavailable：抓不到原文（非 GitHub 仓库 / 无 README / 网络失败），退回 fallback_description
    """

    card_id: str
    source_url: Optional[str] = None
    source_platform: Optional[str] = None
    content: Optional[str] = Field(
        None, description="仓库 README 原文（Markdown），超出上限会被截断"
    )
    truncated: bool = False
    meta: Optional[CardContentMeta] = None
    fallback_description: Optional[str] = Field(
        None, description="抓不到原文时的兜底展示：采集时的原始简介"
    )
    origin: Literal["snapshot", "on_demand", "unavailable"] = "unavailable"


# ==================== 中文导读（无中文 README 的兜底） ====================


class CardDigestResponse(BaseModel):
    """GET /discover/cards/{card_id}/digest 响应（需登录）

    大量仓库 README 只有英文，中文用户点开「先读原文」等于读不懂。这里给一份
    **中文导读**：不是逐字译文，而是「这仓库是什么、解决什么、适合谁」的中文概览。

    `origin` 决定前端展示方式：
    - cache：命中已生成的导读（且原文版本未变）→ 零成本秒回
    - generated：本次实时生成并已回写，查看原文后可再点一次即为 cache
    - unavailable：生成不了，看 `reason`（no_readme / generation_failed / ...），
      并用 fallback_description 优雅降级
    """

    card_id: str
    digest: Optional[str] = Field(None, description="中文导读正文（Markdown）")
    source_fingerprint: Optional[str] = Field(
        None, description="生成时原文版本的指纹（git blob sha，兜底为内容摘要）"
    )
    generated_at: Optional[str] = Field(None, description="生成时间（ISO 8601）")
    fallback_description: Optional[str] = Field(
        None, description="生成不了时的兜底展示：采集时的原始简介"
    )
    origin: Literal["cache", "generated", "unavailable"] = "unavailable"
    reason: Optional[str] = Field(
        None, description="origin=unavailable 时的原因，便于前端给出准确提示"
    )
