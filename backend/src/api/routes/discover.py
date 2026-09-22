# Discover Routes - News Discovery API
"""
资讯发现 API
- GET /api/v1/discover/news: 获取推荐资讯列表
- GET /api/v1/discover/articles: 获取推荐文章列表

契约：
- 所有路由声明 response_model（见 src/schemas/discover.py）
- news / articles 采用一致的降级链：知识库 -> 缓存 -> 内置示例，用 source 字段区分
- 业务异常统一抛 ILOException，由全局处理器归一为 {code, message, detail}
"""

from fastapi import APIRouter, Depends, Query, Request
from typing import List, Dict, Any, Optional
import json
import os
import random
import redis as redis_lib
from loguru import logger

from src.api.deps import CurrentUser, OptionalUser
from src.core.config import settings
from src.core.errors import ERROR_RESPONSES, ILOException
from src.core.rate_limit import (
    CARD_CONTENT_IP,
    CARD_DIGEST_IP,
    DISCOVER_REFRESH_IP,
    NEWS_IP,
    rate_limit,
)
from src.schemas.discover import (
    CardContentResponse,
    CardDigestResponse,
    CardListResponse,
    ProvenanceResponse,
    RefreshResponse,
    TaggedNewsResponse,
    TrendingResponse,
)
from src.services.activity_service import log_activity

router = APIRouter(prefix="/discover", tags=["Discovery"], responses=ERROR_RESPONSES)


# Redis 客户端（懒加载，用于卡片池缓存）
_redis_client = None


def get_redis_client():
    global _redis_client
    if _redis_client is None:
        _redis_client = redis_lib.Redis.from_url(
            os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
        )
    return _redis_client


def save_feed_cache(items: List[Dict[str, Any]]):
    """把抓取到的卡片池写入 Redis（TTL 24 小时）"""
    try:
        r = get_redis_client()
        r.setex("news:feed", 86400, json.dumps(items, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"[WARN] 无法写入 Redis feed 缓存: {e}")


def get_feed_cache() -> Optional[List[Dict[str, Any]]]:
    """从 Redis 读取卡片池"""
    try:
        r = get_redis_client()
        data = r.get("news:feed")
        return json.loads(data) if data else None
    except Exception:
        return None


# 内置示例数据 - 精简版（简介 100 字内）
FALLBACK_NEWS = [
    {
        "id": "news-001",
        "title": "Pydantic v2: 革命性数据验证框架",
        "summary": "性能提升 10 倍的新特性，包括更快的解析速度、更好的类型提示支持和全新的 API 设计。支持自定义验证器、模型配置和序列化优化，是 Python 数据处理的必学工具。",
        "tags": ["backend", "python", "validation"],
        "core_concepts": ["validators", "model-config", "serialization"],
        "source": "TechCrunch",
        "created_at": "2026-09-03T07:00:00Z"
    },
    {
        "id": "news-002",
        "title": "Rust 1.75 发布：内存安全的新里程碑",
        "summary": "最新的 Rust 版本带来了宏系统的重写和更好的错误提示信息，继续推动内存安全在系统编程领域的普及，性能表现优异且社区生态蓬勃发展。",
        "tags": ["systems", "rust", "memory"],
        "core_concepts": ["macros", "error-handling", "ownership"],
        "source": "GitHub Trending",
        "created_at": "2026-09-02T14:30:00Z"
    },
    {
        "id": "news-003",
        "title": "React 19 新特性预览",
        "summary": "Server Components 正式合并，Composition API 的 JavaScript 实现，以及更细粒度的响应式系统，大幅提升应用性能和开发者体验。",
        "tags": ["frontend", "react", "javascript"],
        "core_concepts": ["server-components", "composition-api", "signals"],
        "source": "React Blog",
        "created_at": "2026-09-02T10:15:00Z"
    }
]


# 内置示例文章 - 知识库不可用时的降级数据（保证 demo 可用）
FALLBACK_ARTICLES = [
    {
        "id": "article-001",
        "type": "article",
        "title": "为什么 RAG 正在重塑企业知识管理",
        "summary": "检索增强生成把大模型的推理能力与企业私有知识结合，在准确率与可解释性之间取得平衡，成为落地量最大的 AI 应用形态之一。",
        "tags": ["ai_ml", "RAG"],
        "core_concepts": ["retrieval", "embedding", "vector-db"],
        "source": "Hacker News",
        "score": 480,
        "comments": 96,
        "published_at": "2026-09-10T08:00:00Z"
    },
    {
        "id": "article-002",
        "type": "article",
        "title": "PostgreSQL 还是 SQLite：中小项目的数据库选型",
        "summary": "从并发写入、运维成本与迁移难度三个维度对比两款数据库，给出不同规模项目下的选型建议与常见误区。",
        "tags": ["database", "PostgreSQL"],
        "core_concepts": ["OLTP", "WAL", "concurrency"],
        "source": "dev.to",
        "score": 275,
        "comments": 54,
        "published_at": "2026-09-09T14:30:00Z"
    },
    {
        "id": "article-003",
        "type": "article",
        "title": "把类型系统用到极致：TypeScript 的边界设计",
        "summary": "通过条件类型、模板字面量类型与品牌类型，在编译期消灭接口误用，让重构在大型前端项目里变得安全可预测。",
        "tags": ["frontend", "TypeScript"],
        "core_concepts": ["conditional-types", "branded-types"],
        "source": "Lobsters",
        "score": 210,
        "comments": 38,
        "published_at": "2026-09-08T09:15:00Z"
    }
]


# 个性化排序的候选池倍数：池子要比首屏条数大得多，「换一批」才有腾挪空间；
# 太小等于每次返回同一批，太大则会推出一堆低相关卡片（池子里越低分越靠后）。
_POOL_FACTOR = 4
_POOL_MIN = 12


def _personalized_items(kb, user, limit: int) -> List[Dict[str, Any]]:
    """登录且画像可用时按兴趣相似度取一批卡片；否则返回 []（调用方回退随机）

    为什么要「随机窗口」而不是直接取 top-k：前端「换一批」是同一个 URL（不带 offset）
    重复请求，若每次都返回确定性 top-k，按钮就变成了假动作（内容不变只弹 toast）。
    这里先按相似度取出一个较大的池子（已做类别打散），再从池子里随机截一段，
    既保持「只在相关卡片里选」，又让每次刷新真的不同。
    """
    if user is None or not settings.interest_profile_enabled:
        return []

    try:
        from src.services.interest_profile import build_user_profile
        from src.services.recommend import diversify_by_category, rank_candidates

        profile = build_user_profile(user.id)
        if profile is None:
            return []  # 新用户 / 无行为 / 知识库不可用：交给随机兜底

        pool_size = max(limit * _POOL_FACTOR, _POOL_MIN)
        pool = rank_candidates(kb.candidate_points(item_type="repo"), profile, pool_size)
        if len(pool) <= limit:
            return pool

        start = random.randint(0, len(pool) - limit)
        return diversify_by_category(pool[start:start + limit])
    except Exception as e:  # noqa: BLE001 - 个性化失败必须不影响首屏可用
        logger.warning(f"[WARN] 个性化排序失败，回退随机: {e}")
        return []


@router.get(
    "/news",
    response_model=CardListResponse,
    dependencies=[Depends(rate_limit(NEWS_IP))],
)
async def get_recommended_news(
    user: OptionalUser,
    limit: int = 3,
    offset: int = 0
) -> Dict[str, Any]:
    """
    获取推荐资讯列表（登录且有画像时按兴趣排序，否则从知识库随机抽取「换一批」秒回）

    ## 参数
    - limit: 返回数量限制（默认 3）
    - offset: 分页偏移量（仅内置示例数据的分支使用）

    个性化是**尽力而为**：未登录、新用户（无行为）、知识库不可用、画像全为退化向量
    都回退到随机抽样，且 `source` 保持 knowledge_base —— 前端不需要感知差异，
    推荐字段为 null 即表示本次不是个性化结果。
    """
    # 优先从 Qdrant 知识库取卡片（已抓取过的技术秒回，无需 LLM）
    items = None
    total = 0
    try:
        from src.modules.discovery.tech_knowledge import get_knowledge_base
        kb = get_knowledge_base()
        total = kb.count("repo")
        items = _personalized_items(kb, user, limit) or kb.sample(limit, item_type="repo")
    except Exception as e:
        logger.warning(f"[WARN] 知识库不可用: {e}")

    if items:
        return {
            "items": items,
            "count": len(items),
            "total": total,
            "has_more": total > limit,
            "source": "knowledge_base",
        }

    # 知识库为空时回退到 Redis 旧缓存
    pool = get_feed_cache()
    if pool:
        sample = random.sample(pool, min(limit, len(pool)))
        return {
            "items": sample,
            "count": len(sample),
            "total": len(pool),
            "has_more": len(pool) > limit,
            "source": "github",
        }

    # 最终回退到内置示例数据（保证 demo 可用）
    recommended = FALLBACK_NEWS[offset:offset + limit]
    return {
        "items": recommended,
        "count": len(recommended),
        "total": len(FALLBACK_NEWS),
        "has_more": offset + limit < len(FALLBACK_NEWS),
        "source": "sample",
    }


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    dependencies=[Depends(rate_limit(DISCOVER_REFRESH_IP))],
)
async def refresh_news(
    user: OptionalUser,
    limit: int = Query(50, ge=1, le=100),
    force: bool = False,
) -> Dict[str, Any]:
    """手动触发抓取 GitHub 热门仓库（自动翻页），去重后只对新仓库做摘要，存入知识库

    - force=True 时忽略去重，对抓到的仓库全部重新摘要并覆盖写入（用于升级摘要规范/向量）
    - 抓取过程统一走 collection_service：批次、溯源记录、去重真相源都落在 PostgreSQL，
      未登录也允许触发（demo 场景），此时批次不记操作人
    - limit 限定 1..100：上游翻页上限 10 页 × per_page 100，但单次请求不该任意放大，
      否则匿名调用方就能用一次请求把外部 API 与 LLM 成本推到上限
    """
    from src.modules.discovery.github_fetcher import GitHubFetcher, attach_readmes
    from src.modules.discovery.tech_knowledge import get_knowledge_base
    from src.services.collection_service import BATCH_TRIGGER_MANUAL, collect_items

    fetcher = GitHubFetcher()
    try:
        try:
            kb = get_knowledge_base()
        except Exception as kb_error:
            # 知识库不可用（Qdrant 未启动）时降级为 Redis 缓存
            logger.warning(f"[WARN] 知识库不可用，降级为 Redis 缓存: {kb_error}")
            kb = None

        cached: List[Dict[str, Any]] = []

        async def _store(item: Dict[str, Any]) -> None:
            """知识库可用时写入 Qdrant，否则先收进内存待会儿落 Redis"""
            if kb is not None:
                await kb.upsert(item)
            else:
                cached.append(item)

        result = await collect_items(
            kind="repo",
            fetch=lambda: fetcher.fetch_trending_repos(limit=limit),
            store=_store,
            trigger=BATCH_TRIGGER_MANUAL,
            operator_user_id=user.id if user is not None else None,
            params={"limit": limit, "force": force},
            force=force,
            # 降级路径的卡片并没进知识库，不能标记已采集，否则知识库恢复后会被永久跳过
            persist=kb is not None,
            # 新卡的 README 原文快照：在去重之后执行，只为真正入库的卡片付费
            enrich=attach_readmes,
        )

        if kb is None:
            save_feed_cache(cached)
            logger.info(f"✅ 抓取完成（Redis 降级）：{len(cached)} 条")
            return {
                "status": "ok",
                "new_count": result.new_count,
                "skipped_count": result.skipped_count,
                "count": len(cached),
                "fallback": "redis",
                "batch_id": result.batch_id,
            }

        if result.status == "empty":
            return {
                "status": "empty",
                "count": 0,
                "message": result.message,
                "batch_id": result.batch_id,
            }

        logger.info(
            f"✅ 抓取完成：新增 {result.new_count} 条，跳过 {result.skipped_count} 条，"
            f"知识库仓库共 {kb.count('repo')} 条"
        )
        return {
            "status": "ok",
            "new_count": result.new_count,
            "skipped_count": result.skipped_count,
            "total_in_kb": kb.count("repo"),
            "batch_id": result.batch_id,
        }
    except Exception as e:
        logger.error(f"❌ 抓取失败: {e}")
        raise ILOException("REFRESH_FAILED", f"抓取失败：{e}", status_code=502)
    finally:
        await fetcher.close()


@router.get("/articles", response_model=CardListResponse)
async def get_recommended_articles(limit: int = 3) -> Dict[str, Any]:
    """
    获取推荐文章列表（从知识库随机抽取 type=article，实现「换一批」秒回）

    与 /news 一致的降级契约：知识库不可用/为空时回退到内置示例文章，
    用 source 字段区分来源（knowledge_base | sample），不再抛 503。

    ## 参数
    - limit: 返回数量限制（默认 3）
    """
    try:
        from src.modules.discovery.tech_knowledge import get_knowledge_base
        kb = get_knowledge_base()
        items = kb.sample(limit, item_type="article")
        total = kb.count("article")
        if items:
            return {
                "items": items,
                "count": len(items),
                "total": total,
                "has_more": total > limit,
                "source": "knowledge_base",
            }
    except Exception as e:
        logger.warning(f"[WARN] 知识库不可用: {e}")

    # 知识库为空/不可用：回退内置示例文章（保证 demo 可用）
    sample = FALLBACK_ARTICLES[:limit]
    return {
        "items": sample,
        "count": len(sample),
        "total": len(FALLBACK_ARTICLES),
        "has_more": len(FALLBACK_ARTICLES) > limit,
        "source": "sample",
    }


@router.post(
    "/refresh-articles",
    response_model=RefreshResponse,
    dependencies=[Depends(rate_limit(DISCOVER_REFRESH_IP))],
)
async def refresh_articles(
    user: OptionalUser,
    # per_platform 与 time_range 都会直接放大下游成本：前者决定每个平台抓多少条、
    # 进而决定要跑多少次 LLM 摘要，后者决定平台侧的时间窗。匿名即可触发，若不给
    # 上下界，一个 `per_platform=100000` 就能把抓取配额与 LLM 额度一次性打空。
    per_platform: int = Query(10, ge=1, le=30),
    time_range: str = Query("day", pattern="^(day|week|month)$"),
    force: bool = False,
) -> Dict[str, Any]:
    """手动触发抓取多平台热门技术文章，去重后只对新文章做摘要，存入知识库

    - force=True 时忽略去重，对抓到的文章全部重新摘要并覆盖写入（用于升级摘要规范/向量）
    - 与 /refresh 共用 collection_service，一次调用 = 一条采集批次 + 每张卡片一条溯源记录
    - 与 /refresh 共用同一 IP 节流桶：两者都是「触发外抓 + LLM 摘要」的成本放大面，
      分开配额等于给同一件事开两条通道
    """
    from src.modules.discovery.article_fetcher import ArticleFetcher
    from src.modules.discovery.tech_knowledge import get_knowledge_base
    from src.services.collection_service import BATCH_TRIGGER_MANUAL, collect_items

    fetcher = ArticleFetcher()
    try:
        kb = get_knowledge_base()

        result = await collect_items(
            kind="article",
            fetch=lambda: fetcher.fetch_articles(per_platform=per_platform, time_range=time_range),
            store=kb.upsert,
            trigger=BATCH_TRIGGER_MANUAL,
            operator_user_id=user.id if user is not None else None,
            params={"per_platform": per_platform, "time_range": time_range, "force": force},
            force=force,
        )

        if result.status == "empty":
            return {
                "status": "empty",
                "count": 0,
                "message": result.message or "各平台均未返回数据，可能网络不可达",
                "batch_id": result.batch_id,
            }

        logger.info(
            f"✅ 文章抓取完成：新增 {result.new_count} 条，跳过 {result.skipped_count} 条，"
            f"知识库文章共 {kb.count('article')} 条"
        )
        return {
            "status": "ok",
            "new_count": result.new_count,
            "skipped_count": result.skipped_count,
            "total_in_kb": kb.count("article"),
            "batch_id": result.batch_id,
        }
    except Exception as e:
        logger.error(f"❌ 文章抓取失败: {e}")
        raise ILOException("REFRESH_ARTICLES_FAILED", f"文章抓取失败：{e}", status_code=502)
    finally:
        await fetcher.close()


@router.get("/trending", response_model=TrendingResponse)
async def get_trending_news() -> Dict[str, Any]:
    """
    获取热门趋势（按热度排序）

    来源：GitHub Trending, Hacker News
    """
    # 按日期降序模拟热度
    sorted_news = sorted(
        FALLBACK_NEWS,
        key=lambda x: x.get("created_at", ""),
        reverse=True
    )

    return {
        "items": sorted_news[:5],
        "updated_at": "2026-09-03T08:00:00Z"
    }


@router.get("/by-tag/{tag}", response_model=TaggedNewsResponse)
async def get_news_by_tag(tag: str, limit: int = 10) -> Dict[str, Any]:
    """
    根据标签过滤资讯

    ## 示例
    - GET /discover/by-tag/python
    - GET /discover/by-tag/frontend
    """
    filtered = [
        item for item in FALLBACK_NEWS
        if tag.lower() in [t.lower() for t in item.get("tags", [])]
    ][:limit]

    return {
        "tag": tag,
        "items": filtered,
        "count": len(filtered)
    }


@router.get("/cards/{card_id}/provenance", response_model=ProvenanceResponse)
async def get_card_provenance(card_id: str, request: Request, user: OptionalUser) -> Dict[str, Any]:
    """查一张卡片的内容溯源：来自哪次采集、哪个链接、摘要生成前的原文是什么

    - 匿名可读（信息本身就是公开的），但不为匿名请求埋点，避免刷流量污染行为时间线
    - 历史卡片与内置示例可能没有采集记录，此时 `known=false` 并照常返回卡片本身，
      让前端能展示「暂无溯源信息」而不是弹错误
    """
    from src.services.collection_service import get_provenance
    from src.services.card_service import find_card

    record = get_provenance(card_id)
    card = find_card(card_id)

    if record is None and card is None:
        raise ILOException(
            "CARD_NOT_FOUND", f"卡片不存在：{card_id}", status_code=404
        )

    # 行为溯源：只有登录用户才记，匿名浏览不落流水（隐私友好，也避免噪声）
    if user is not None:
        log_activity(
            "view_card",
            user_id=user.id,
            target_type="card",
            target_id=card_id,
            request=request,
        )

    if record is None:
        return {
            "item_id": card_id,
            "known": False,
            "card": card,
        }

    return {
        "item_id": record["item_id"],
        "known": True,
        "source_platform": record["source_platform"],
        "source_url": record["source_url"],
        "raw_description": record["raw_description"],
        "collected_at": record["collected_at"],
        "summary_generated_at": record["summary_generated_at"],
        "status": record["status"],
        "backfilled": record["backfilled"],
        "batch": record["batch"],
        # 入库当时的快照 vs 当前卡片：覆盖写入（force 刷新）后两者会不同
        "snapshot": record["card_payload"],
        "card": card,
    }


@router.get(
    "/cards/{card_id}/content",
    response_model=CardContentResponse,
    dependencies=[Depends(rate_limit(CARD_CONTENT_IP))],
)
async def get_card_content(card_id: str) -> Dict[str, Any]:
    """读一张卡片的原文（仓库 README）快照，「先读原文」页的数据来源

    - 匿名可读：README 本身就是公开内容，读原文不该被登录墙拦住（提问才需要登录）
    - 本地没有快照（存量卡片）时按需补抓一次并回写，之后零延迟
    - 抓不到时返回 `origin=unavailable` 并带上 fallback_description，由前端优雅降级
    - 之所以仍要限流：命中「需补抓」分支时这里会发起真实的外网请求，
      card_id 又是路径参数（可任意枚举），不给节流就等于把后端变成匿名可用的代理
    """
    from src.services.collection_service import get_or_fetch_card_content

    return await get_or_fetch_card_content(card_id)


@router.get(
    "/cards/{card_id}/digest",
    response_model=CardDigestResponse,
    dependencies=[Depends(rate_limit(CARD_DIGEST_IP))],
)
async def get_card_digest(card_id: str, user: CurrentUser) -> Dict[str, Any]:
    """读一张卡片的中文导读（无中文 README 时的兜底）

    - **要求登录**：读原文（/content）可以匿名，因为 README 本身就是公开内容；
      但导读每次未命中缓存都会真实调用一次 LLM，是明确的付费放大面 ——
      匿名开放等于让别人拿我们的 API Key 免费翻译，所以这里必须过登录墙。
    - 按需生成 + 持久复用：命中缓存（且原文版本未变）零成本秒回，不重复付费
    - 生成不了时返回 `origin=unavailable` 并带 `reason` 与 fallback_description，
      由前端优雅降级，绝不阻塞「先读原文」这条主链路
    - 仍保留 IP 限流：登录用户也可能被脚本驱动着反复打这个接口
    """
    from src.services.collection_service import get_or_generate_content_digest

    return await get_or_generate_content_digest(card_id)


# 测试运行
if __name__ == "__main__":
    import requests

    # 测试推荐接口
    resp = requests.get("http://localhost:8000/api/v1/discover/news")
    print(f"📊 Recommended: {resp.json()['count']} items")

    # 测试热门接口
    resp = requests.get("http://localhost:8000/api/v1/discover/trending")
    print(f"🔥 Trending: {len(resp.json()['items'])} items")
