# Discover Routes - News Discovery API
"""
资讯发现 API
- GET /api/v1/discover/news: 获取推荐资讯列表
"""

from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any, Optional
import asyncio
import json
import os
import redis as redis_lib
from loguru import logger

router = APIRouter(prefix="/discover", tags=["Discovery"])


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


# Mock 数据 - 精简版（简介 100 字内）
MOCK_NEWS = [
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


@router.get("/news")
async def get_recommended_news(
    limit: int = 3,
    offset: int = 0
) -> Dict[str, Any]:
    """
    获取推荐资讯列表（优先从知识库随机抽取，实现「换一批」秒回）

    ## 参数
    - limit: 返回数量限制（默认 3）
    - offset: 分页偏移量
    """
    # 优先从 Qdrant 知识库随机抽取（已抓取过的技术秒回，无需 LLM）
    items = None
    total = 0
    try:
        from src.modules.discovery.tech_knowledge import get_knowledge_base
        kb = get_knowledge_base()
        items = kb.sample(limit, item_type="repo")
        total = kb.count("repo")
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
        import random
        sample = random.sample(pool, min(limit, len(pool)))
        return {
            "items": sample,
            "count": len(sample),
            "total": len(pool),
            "has_more": len(pool) > limit,
            "source": "github",
        }

    # 最终回退到 Mock（保 demo 可用）
    recommended = MOCK_NEWS[offset:offset + limit]
    return {
        "items": recommended,
        "count": len(recommended),
        "total": len(MOCK_NEWS),
        "has_more": offset + limit < len(MOCK_NEWS),
        "source": "mock",
    }


@router.post("/refresh")
async def refresh_news(limit: int = 50, force: bool = False) -> Dict[str, Any]:
    """手动触发抓取 GitHub 热门仓库（自动翻页），去重后只对新仓库做摘要，存入知识库

    - force=True 时忽略去重，对抓到的仓库全部重新摘要并覆盖写入（用于升级摘要规范/向量）
    """
    from src.modules.discovery.github_fetcher import GitHubFetcher, summarize_items
    from src.modules.discovery.tech_knowledge import get_knowledge_base

    fetcher = GitHubFetcher()
    try:
        repos = await fetcher.fetch_trending_repos(limit=limit)
        if not repos:
            return {
                "status": "empty",
                "count": 0,
                "message": "GitHub 未返回数据，可能触发限流或网络不可达",
            }

        try:
            kb = get_knowledge_base()
            # 去重：只处理未抓过的仓库（已抓过的直接复用知识库，零 LLM 成本）；force 时全量重摘要覆盖
            if force:
                new_repos = list(repos)
            else:
                new_repos = [r for r in repos if not kb.is_crawled(r["id"])]
            skipped = len(repos) - len(new_repos)

            # 只对新仓库做结构化摘要
            new_repos = await summarize_items(new_repos, kind="repo")

            # 存入知识库 + 标记已抓
            for r in new_repos:
                await kb.upsert(r)
                kb.mark_crawled(r["id"])

            logger.info(f"✅ 抓取完成：新增 {len(new_repos)} 条，跳过 {skipped} 条已存在，知识库仓库共 {kb.count('repo')} 条")
            return {
                "status": "ok",
                "new_count": len(new_repos),
                "skipped_count": skipped,
                "total_in_kb": kb.count("repo"),
            }
        except Exception as kb_error:
            # 知识库不可用（Qdrant 未启动）时降级为 Redis 缓存
            logger.warning(f"[WARN] 知识库不可用，降级为 Redis 缓存: {kb_error}")
            items = await summarize_items(repos)
            save_feed_cache(items)
            return {"status": "ok", "count": len(items), "fallback": "redis"}
    except Exception as e:
        logger.error(f"❌ 抓取失败: {e}")
        raise HTTPException(status_code=502, detail=f"抓取失败: {e}")
    finally:
        await fetcher.close()


@router.get("/articles")
async def get_recommended_articles(limit: int = 3) -> Dict[str, Any]:
    """
    获取推荐文章列表（从知识库随机抽取 type=article，实现「换一批」秒回）

    ## 参数
    - limit: 返回数量限制（默认 3）
    """
    try:
        from src.modules.discovery.tech_knowledge import get_knowledge_base
        kb = get_knowledge_base()
        items = kb.sample(limit, item_type="article")
        total = kb.count("article")
        return {
            "items": items,
            "count": len(items),
            "total": total,
            "has_more": total > limit,
            "source": "knowledge_base",
        }
    except Exception as e:
        logger.warning(f"[WARN] 知识库不可用: {e}")
        raise HTTPException(status_code=503, detail=f"知识库不可用: {e}")


@router.post("/refresh-articles")
async def refresh_articles(per_platform: int = 10, time_range: str = "day", force: bool = False) -> Dict[str, Any]:
    """手动触发抓取多平台热门技术文章，去重后只对新文章做摘要，存入知识库

    - force=True 时忽略去重，对抓到的文章全部重新摘要并覆盖写入（用于升级摘要规范/向量）
    """
    from src.modules.discovery.article_fetcher import ArticleFetcher
    from src.modules.discovery.github_fetcher import summarize_items
    from src.modules.discovery.tech_knowledge import get_knowledge_base

    fetcher = ArticleFetcher()
    try:
        articles = await fetcher.fetch_articles(per_platform=per_platform, time_range=time_range)
        if not articles:
            return {
                "status": "empty",
                "count": 0,
                "message": "各平台均未返回数据，可能网络不可达",
            }

        kb = get_knowledge_base()
        # 去重：只处理未抓过的文章；force 时全量重摘要覆盖
        if force:
            new_articles = list(articles)
        else:
            new_articles = [a for a in articles if not kb.is_crawled(a["id"])]
        skipped = len(articles) - len(new_articles)

        # 只对新文章做结构化摘要
        new_articles = await summarize_items(new_articles, kind="article")

        # 存入知识库 + 标记已抓
        for a in new_articles:
            await kb.upsert(a)
            kb.mark_crawled(a["id"])

        logger.info(f"✅ 文章抓取完成：新增 {len(new_articles)} 条，跳过 {skipped} 条，知识库文章共 {kb.count('article')} 条")
        return {
            "status": "ok",
            "new_count": len(new_articles),
            "skipped_count": skipped,
            "total_in_kb": kb.count("article"),
        }
    except Exception as e:
        logger.error(f"❌ 文章抓取失败: {e}")
        raise HTTPException(status_code=502, detail=f"文章抓取失败: {e}")
    finally:
        await fetcher.close()


@router.get("/trending")
async def get_trending_news() -> Dict[str, Any]:
    """
    获取热门趋势（按热度排序）
    
    来源：GitHub Trending, Hacker News
    """
    # 按日期降序模拟热度
    sorted_news = sorted(
        MOCK_NEWS,
        key=lambda x: x.get("created_at", ""),
        reverse=True
    )
    
    return {
        "items": sorted_news[:5],
        "updated_at": "2026-09-03T08:00:00Z"
    }


@router.get("/by-tag/{tag}")
async def get_news_by_tag(tag: str, limit: int = 10) -> Dict[str, Any]:
    """
    根据标签过滤资讯
    
    ## 示例
    - GET /discover/by-tag/python
    - GET /discover/by-tag/frontend
    """
    filtered = [
        item for item in MOCK_NEWS
        if tag.lower() in [t.lower() for t in item.get("tags", [])]
    ][:limit]
    
    return {
        "tag": tag,
        "items": filtered,
        "count": len(filtered)
    }


# 测试运行
if __name__ == "__main__":
    import requests
    
    # 测试推荐接口
    resp = requests.get("http://localhost:8000/api/v1/discover/news")
    print(f"📊 Recommended: {resp.json()['count']} items")
    
    # 测试热门接口
    resp = requests.get("http://localhost:8000/api/v1/discover/trending")
    print(f"🔥 Trending: {len(resp.json()['items'])} items")
