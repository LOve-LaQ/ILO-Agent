# Discover Schemas - 资讯发现接口契约
"""
- CardListResponse: GET /discover/news 与 /discover/articles 的统一响应
- RefreshResponse: POST /discover/refresh 与 /discover/refresh-articles 的响应
- ProvenanceResponse: GET /discover/cards/{card_id}/provenance 的内容溯源响应
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

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
