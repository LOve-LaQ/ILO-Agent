# Discover Schemas - 资讯发现接口契约
"""
- CardListResponse: GET /discover/news 与 /discover/articles 的统一响应
- TaskAcceptedResponse: POST /discover/refresh 与 /refresh-articles 的受理响应（202）
- TaskProgressResponse: GET /discover/tasks/{task_id} 的任务进度响应
- ProvenanceResponse: GET /discover/cards/{card_id}/provenance 的内容溯源响应
- CardContentResponse: GET /discover/cards/{card_id}/content 的原文快照响应
- SimilarResponse: GET /discover/cards/{card_id}/similar 的相似推荐响应
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


class TaskAcceptedResponse(BaseModel):
    """采集任务已受理（202）

    `task_id` 同时是 `collection_batches.id`：前端只需记一个标识，进程重启后
    这个 id 依然能查到批次终态。
    """

    task_id: str = Field(..., description="任务号，等于采集批次号")
    kind: str = Field(..., description="repo | article")
    status: str = Field("running", description="受理时恒为 running")
    stage: str = Field("queued", description="当前阶段")
    stage_label: str = Field("任务已提交…", description="阶段的中文文案")
    poll_url: str = Field(..., description="查询进度的完整路径，前端无需自行拼接")
    reused: bool = Field(
        False,
        description="是否复用了已有任务（并发触发时返回 409，此字段仅用于同步语义扩展）",
    )


class TaskProgressResponse(BaseModel):
    """采集任务进度（轮询端每 0.8~1.5 秒拉一次）

    进度是**服务端事实**：`stage` 由采集链路自行上报，`processed/total` 来自
    真实的批次循环。进程重启导致内存进度丢失时，接口会用批次表回填终态，
    并以 `progress_lost=true` 明确标注「过程不可得，结局可得」。
    """

    task_id: str
    kind: str
    status: str = Field(..., description="running | succeeded | partial | failed | empty")
    stage: str
    stage_label: str
    processed: int = 0
    total: int = 0
    percent: int = Field(0, ge=0, le=100)
    new_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    message: Optional[str] = None
    error: Optional[str] = None
    elapsed_ms: int = 0
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    progress_lost: bool = Field(
        False, description="true 表示内存进度已随进程重启丢失，数据来自批次表终态"
    )


# ==================== GitHub 新星榜 ====================


class RankingLanguage(BaseModel):
    """榜单语言筛选项（后端是唯一事实源，前端不自己维护列表）"""

    value: str = Field(..., description="参数值，如 all / python / cpp")
    label: str = Field(..., description="展示名，同时是 GitHub 的 language 检索值")


class RankingItem(BaseModel):
    """榜单一行

    只有 `stars` 是 GitHub 的权威数字；`stars_per_day` 是本地派生的热度指标
    （star 总数 / 仓库存活天数），用来区分「3 天 2000 星」和「7 天 2100 星」。
    """

    rank: int = Field(..., ge=1)
    repo_id: int
    full_name: str = Field(..., description="owner/repo")
    url: str
    description: Optional[str] = Field(
        None, description="GitHub 上的原始简介（多为英文，未经 LLM 改写）"
    )
    summary_cn: Optional[str] = Field(
        None,
        description=(
            "已解读项目的中文摘要（来自知识库）；有值时前端优先展示它 —— "
            "榜单里一半是英文原文、一半是中文摘要，读起来会很割裂"
        ),
    )
    stars: int = 0
    forks: int = 0
    language: Optional[str] = None
    topics: List[str] = []
    created_at: Optional[datetime] = None
    pushed_at: Optional[datetime] = None
    age_hours: Optional[float] = Field(None, description="仓库存活小时数，用于折算热度")
    stars_per_day: Optional[float] = Field(
        None, description="派生指标：star 总数 / 存活天数"
    )
    card_id: str = Field(..., description="与知识库卡片同构的 id（gh-<repo_id>）")
    has_explanation: bool = Field(
        False, description="该仓库是否已在知识库中（即已有中文摘要可读）"
    )


class RankingResponse(BaseModel):
    """GET /discover/rankings 响应

    口径（前端照抄展示）：窗口内**创建**的仓库，按 star 总数降序。
    数据来源见 `source`：github 实时 / cache 缓存 / stale 过期缓存 /
    knowledge_base 本机快照（非实时排名）/ unavailable 无数据。
    """

    window: str = Field(..., description="week | month")
    language: str = "all"
    window_start: Optional[str] = Field(None, description="统计起点（ISO 日期）")
    items: List[RankingItem] = []
    total: int = Field(0, description="窗口内符合条件的仓库总数（GitHub total_count）")
    query: Optional[str] = Field(None, description="实际下发的 GitHub 检索串，便于复现")
    source: Literal["github", "cache", "stale", "knowledge_base", "unavailable"]
    stale: bool = Field(False, description="是否为过期数据（GitHub 不可达时的兜底）")
    fetched_at: Optional[datetime] = None
    languages: List[RankingLanguage] = []
    note: Optional[str] = Field(None, description="口径说明或降级说明，前端原样展示")


class SearchFilters(BaseModel):
    """本次搜索实际生效的筛选条件（回显，避免用户误以为筛选项没生效）"""

    language: str = "all"
    min_stars: int = 0
    sort: Literal["stars", "updated"] = "stars"


class SearchResponse(BaseModel):
    """GET /discover/search 响应

    与 RankingResponse 的三点差异（都会影响前端文案，故写在这里）：

    1. **总量不承诺准确**：GitHub 的 `total_count` 是估算值，搜索只取一页，
       所以 `total` 只用于「约 N 个结果」这种措辞，不做分页。
    2. **降级不能用全库快照**：榜单兜底可以「按星数排本地已收录仓库」，但搜索
       的语义是「与这个词相关」，拿全库按星排就是答非所问。这里的 knowledge_base
       兜底只返回**文案里真的命中关键词**的仓库，宁可为空。
    3. **语法错误不降级**：查询串被 GitHub 拒绝（422）时直接报错让用户改词，
       而不是端出一份缓存结果。
    """

    keyword: str = Field(..., description="归一化后的检索词（回显）")
    github_query: Optional[str] = Field(
        None, description="实际下发的 GitHub 检索串，便于理解结果口径与复现"
    )
    items: List[RankingItem] = []
    total: int = Field(0, description="GitHub 给出的结果总数（估算值，非分页承诺）")
    filters: SearchFilters = SearchFilters()
    source: Literal["github", "cache", "stale", "knowledge_base", "unavailable"]
    stale: bool = Field(False, description="是否为过期数据（GitHub 不可达时的兜底）")
    fetched_at: Optional[datetime] = None
    languages: List[RankingLanguage] = []
    note: Optional[str] = Field(None, description="口径说明或降级说明，前端原样展示")


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
    trigger: str = Field(..., description="manual | scheduled | script")
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


# ==================== 相似推荐（向量近邻） ====================


class SimilarItem(BaseModel):
    """相似条目的展示字段

    `similarity` 是**原始余弦相似度**，不是排序分：排序时对「共享概念 / 同方向」
    有极小加分，但那个分数只用于决定先后，对外展示仍以语义相似度为准。
    """

    card_id: str = Field(..., description="知识库卡片 id，前端据此续接「学习」抽屉")
    title: str
    url: Optional[str] = None
    summary_cn: Optional[str] = Field(None, description="中文摘要（入库时生成，可直接展示）")
    language: Optional[str] = None
    stars: Optional[int] = None
    type: Literal["repo", "article"] = "repo"
    category: str = Field("other", description="方向代码，用于展示与技术归类")
    core_concepts: List[str] = Field(
        default_factory=list,
        description="完整技术概念（不是交集）：续接学习会话时要传给模型",
    )
    similarity: float = Field(..., ge=0, le=1, description="余弦相似度（保留三位小数）")
    reason: str = Field(..., description="一句中文理由：共享哪些概念 / 同属哪个方向")
    shared_concepts: List[str] = Field(
        default_factory=list, description="与源卡片共享的技术概念（最多 4 个）"
    )


class SimilarResponse(BaseModel):
    """GET /discover/cards/{card_id}/similar 响应

    - `based_on=vector`：正常，按向量近邻召回（items 可能为空 —— 语义上确实没有
      足够相近的项目，此时**不该**拿不相关的结果凑数）
    - `based_on=unavailable`：源卡片没有入库向量或 Qdrant 不可用，`reason` 说明原因，
      前端整块隐藏（不是报错：相似推荐是增强，不是主链路）
    """

    card_id: str
    based_on: Literal["vector", "unavailable"] = "vector"
    reason: Optional[str] = Field(
        None, description="based_on=unavailable 时的原因：no_vector | unavailable"
    )
    min_score: float = Field(..., description="本次采用的相似度下限，便于解释「为什么只有两条」")
    items: List[SimilarItem] = []

