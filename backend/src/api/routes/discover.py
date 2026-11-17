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

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from typing import Awaitable, Callable, Dict, Any, List, Optional
import asyncio
import hashlib
import json
import random
import uuid
import redis as redis_lib
from loguru import logger

from src.api.deps import CurrentUser, OptionalUser
from src.core.config import settings
from src.core.errors import ERROR_RESPONSES, ILOException
from src.core.net import client_ip, rate_limit_key
from src.core.rate_limit import (
    CARD_CONTENT_IP,
    CARD_DIGEST_IP,
    DISCOVER_REFRESH_IP,
    NEWS_IP,
    RANKING_IP,
    SEARCH_IP,
    SIMILAR_IP,
    rate_limit,
)
from src.core.redis_client import get_redis as get_shared_redis
from src.schemas.discover import (
    CardContentResponse,
    CardDigestResponse,
    CardListResponse,
    ProvenanceResponse,
    RankingResponse,
    SearchResponse,
    SimilarResponse,
    TaggedNewsResponse,
    TaskAcceptedResponse,
    TaskProgressResponse,
    TrendingResponse,
)
from src.services import task_runner
from src.services.activity_service import log_activity_async
from src.services.collection_service import (
    BATCH_TRIGGER_MANUAL,
    get_batch,
    start_batch,
)
from src.services.feature_flags import is_enabled, require_feature
from src.services.warmup import schedule_heavy_warmup

router = APIRouter(prefix="/discover", tags=["Discovery"], responses=ERROR_RESPONSES)


# Redis 客户端（懒加载，用于卡片池缓存）
_redis_client = None


def get_redis_client():
    """同步 Redis 客户端（懒加载），用于卡片池缓存。

    必须配 `socket_timeout`：不配等于无限等。Redis 只是卡片池的热缓存，
    不可达时该降级就得能降下去，而不是把整条链路卡死。

    调用点注意：这是**同步**客户端，在 `async def` 路由里必须用
    `await asyncio.to_thread(...)` 卸载 —— 直接调会阻塞事件循环。
    """
    global _redis_client
    if _redis_client is None:
        # 走 settings 而不是 os.getenv：pydantic-settings 同时覆盖进程环境与 .env 文件，
        # 优先级也是「进程环境 > .env > 默认值」，是 os.getenv 的超集。
        _redis_client = redis_lib.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=settings.redis_socket_timeout,
            socket_connect_timeout=settings.redis_socket_timeout,
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


async def _personalized_items(
    kb, user, limit: int, deadline: Optional[float] = None
) -> List[Dict[str, Any]]:
    """登录且画像可用时按兴趣相似度取一批卡片；否则返回 []（调用方回退随机）

    是 `async def` 而非同步函数：内部 `build_user_profile`（DB + Redis + Qdrant）
    与 `candidate_points`（Qdrant scroll）都是同步 IO，必须卸载到线程池。
    这条链跑在 `/news` 首页接口上，直接调用会阻塞事件循环、放大并发 P95。

    为什么要「随机窗口」而不是直接取 top-k：前端「换一批」是同一个 URL（不带 offset）
    重复请求，若每次都返回确定性 top-k，按钮就变成了假动作（内容不变只弹 toast）。
    这里先按相似度取出一个较大的池子（已做类别打散），再从池子里随机截一段，
    既保持「只在相关卡片里选」，又让每次刷新真的不同。
    """
    if user is None or not settings.interest_profile_enabled:
        return []

    try:
        from src.services.interest_profile import build_user_profile
        from src.services.recommend import diversify_by_category, rank_candidates, rank_pool

        profile = await asyncio.to_thread(build_user_profile, user.id, deadline=deadline)
        if profile is None:
            return []  # 新用户 / 无行为 / 知识库不可用：交给随机兜底

        pool_size = max(limit * _POOL_FACTOR, _POOL_MIN)
        # 候选池带进程内缓存（命中时不碰 Qdrant），但仍然只能在 to_thread 里调用：
        # 未命中时会真的发一次带向量的 scroll（~450ms），裸调会独占事件循环。
        pool = await asyncio.to_thread(kb.candidate_pool, item_type="repo")
        if pool is not None:
            # 快速通道：矩阵乘一次算完 500 条余弦（~1ms），不做 Python 逐条装箱
            ranked = rank_pool(pool["payloads"], pool["matrix"], profile, pool_size)
        else:
            # 无 numpy / 池构建失败：回到逐条排序，语义相同只是慢
            candidates = await asyncio.to_thread(kb.candidate_points, item_type="repo")
            ranked = rank_candidates(candidates, profile, pool_size)

        if len(ranked) <= limit:
            return ranked

        start = random.randint(0, len(ranked) - limit)
        return diversify_by_category(ranked[start:start + limit])
    except Exception as e:  # noqa: BLE001 - 个性化失败必须不影响首屏可用
        logger.warning(f"[WARN] 个性化排序失败，回退随机: {e}")
        return []


@router.get(
    "/news",
    response_model=CardListResponse,
    dependencies=[Depends(rate_limit(NEWS_IP))],
)
async def get_recommended_news(
    request: Request,
    user: OptionalUser,
    # 必须有上下界：limit=-1 在 Python 切片里是「去掉最后一条」，limit=1000
    # 会一次序列化上千条卡片 —— 这不是分页，是一个公开的响应体放大口。
    limit: int = Query(3, ge=1, le=100, description="返回条数（1-100，默认 3）"),
    offset: int = Query(0, ge=0, description="分页偏移（仅内置示例分支使用）"),
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
    # 用户拿到首屏之后，再在后台把 LLM 栈（langchain→transformers→torch，本地 12s）
    # 热起来：它只服务「点开卡片学习」，但等到用户真点下去才导入，那一下就是十几秒。
    # 延后 1.5s 是为了让这次响应先发出去 —— 立刻开工会占住 GIL 把首页拖慢。
    schedule_heavy_warmup()

    # 优先从 Qdrant 知识库取卡片（已抓取过的技术秒回，无需 LLM）
    items = None
    total = 0
    try:
        from src.modules.discovery.tech_knowledge import get_knowledge_base
        kb = await asyncio.to_thread(get_knowledge_base)
        total = await asyncio.to_thread(kb.count, "repo")
        personalized = await _personalized_items(
            kb, user, limit, deadline=getattr(request.state, "deadline", None)
        )
        items = personalized or await asyncio.to_thread(kb.sample, limit, item_type="repo")
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
    pool = await asyncio.to_thread(get_feed_cache)
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


# ==================== 采集任务（异步执行 + 幂等） ====================

# 幂等锁 scope：repo 与 article 共用一把锁。两条通道打的是同一个知识库、
# 同一批 LLM 配额，允许并发等于让一次误操作付两份钱。
_REFRESH_SCOPE = "discover:refresh"

# CollectResult.status -> 任务状态。两套词表的含义并不完全对应，必须显式映射：
# `ok` 是「采集成功」，不是「任务成功」；`error` 是「全部入库失败」，
# 任务是跑完了的（所以不能算 crashed）。
_TASK_STATUS = {
    "ok": "succeeded",
    "partial": "partial",
    "error": "failed",
    "empty": "empty",
}


def _task_subject(request: Request, user: Optional[Any]) -> str:
    """幂等锁的主体：登录用户按 id，匿名称 IP 的哈希

    用哈希而不是明文 IP：Redis 里的键不该留存可识别的客户端地址，与限流
    的归一方式保持一致（见 core/net.py 的 rate_limit_key）。
    """
    if user is not None:
        return f"u:{user.id}"
    return f"ip:{rate_limit_key(client_ip(request))}"


def _poll_url(task_id: str) -> str:
    return f"/api/v1/discover/tasks/{task_id}"


async def _start_collect_task(
    *,
    request: Request,
    user: Optional[Any],
    kind: str,
    params: Dict[str, Any],
    runner: Callable[[task_runner.TaskState], Awaitable[None]],
) -> Dict[str, Any]:
    """抢锁 → 建任务 → 起后台执行，返回受理响应体

    请求路径上**只做一件 IO**：抢幂等锁（Redis 一次 SET NX）。开采集批次要写
    PostgreSQL，放到后台任务的第一行做 —— 否则这个「立即返回」的接口还要等
    一次事务提交（实测 1 秒），把异步化的收益吃掉一半。

    代价是：批次号虽然已经确定（= task_id），但批次行可能比受理响应晚几毫秒
    才落库。这不影响前端：进度查的是内存任务表，落库只服务于事后溯源。

    顺序不能反：**先抢锁再起任务**。并发两击若都起来了，就会各开一条批次，
    其中一条立刻变成永远 running 的孤儿，把「最近一次采集成没成」搞乱。
    """
    task_id = str(uuid.uuid4())
    subject = _task_subject(request, user)

    holder = await asyncio.to_thread(
        task_runner.acquire, _REFRESH_SCOPE, subject, task_id=task_id
    )
    if holder is not None:
        logger.info(f"event=task_rejected reason=duplicate holder={holder}")
        raise ILOException(
            "TASK_ALREADY_RUNNING",
            "已经有一个抓取任务在跑，等它结束再试（也可以直接查看它的进度）。",
            status_code=409,
            detail={"task_id": holder, "poll_url": _poll_url(holder)},
        )

    state = task_runner.TaskState(task_id=task_id, kind=kind)
    task_runner.spawn(state, runner, scope=_REFRESH_SCOPE, subject=subject)
    logger.info(f"event=task_submitted task_id={task_id} kind={kind} subject={subject}")

    return {
        "task_id": task_id,
        "kind": kind,
        "status": "running",
        "stage": state.stage,
        "stage_label": task_runner.STAGE_LABELS.get(state.stage, state.stage),
        "poll_url": _poll_url(task_id),
        "reused": False,
    }


def _apply_result(state: task_runner.TaskState, result: Any) -> None:
    """把 CollectResult 映射成任务状态（计数与状态必须来自同一个事实）"""
    state.status = _TASK_STATUS.get(result.status, "failed")
    state.new_count = int(result.new_count or 0)
    state.skipped_count = int(result.skipped_count or 0)
    state.failed_count = int(result.failed_count or 0)
    if result.message:
        state.message = result.message
    if state.status == "partial" and not state.message:
        state.message = f"新增 {state.new_count} 条，另有 {state.failed_count} 条未入库"
    if state.status == "failed" and not state.error:
        state.error = (
            f"{state.new_count} 条新内容全部未能入库"
            f"（摘要未中文化或写入失败，详见服务端日志）"
        )


@router.post(
    "/refresh",
    response_model=TaskAcceptedResponse,
    status_code=202,
    dependencies=[Depends(rate_limit(DISCOVER_REFRESH_IP))],
)
async def refresh_news(
    request: Request,
    user: OptionalUser,
    limit: int = Query(50, ge=1, le=100),
    force: bool = False,
) -> Dict[str, Any]:
    """提交一次 GitHub 热门仓库抓取任务，**立即返回**（202）

    - force=True 时忽略去重，对抓到的仓库全部重新摘要并覆盖写入（用于升级摘要规范/向量）
    - 抓取过程统一走 collection_service：批次、溯源记录、去重真相源都落在 PostgreSQL，
      未登录也允许触发（demo 场景），此时批次不记操作人
    - limit 限定 1..100：上游翻页上限 10 页 × per_page 100，但单次请求不该任意放大，
      否则匿名调用方就能用一次请求把外部 API 与 LLM 成本推到上限

    【为什么是异步】抓取 + 多批摘要整条链路要 1~3 分钟，同步执行会被浏览器与
    反向代理先超时切断 —— 用户看到网络错误并重试，而服务端还在继续抓取和调用
    LLM，等于一次误操作付两遍钱。这里改为立即返回 `task_id`（= 采集批次号），
    由 `GET /discover/tasks/{task_id}` 轮询真实进度。
    **边界**：任务跑在当前进程的事件循环里，多 worker 部署时需要换成外部队列
    （幂等锁已在 Redis 上，不会因此重复触发）。

    【并发触发】同一主体（登录用户 / 匿名 IP）同时只允许一个抓取任务，重复提交
    返回 409 并在 detail 里带上正在跑的那个 task_id —— 前端据此接着看进度，而不是
    傻等或报错。

    【功能开关】`discover_refresh` 关闭时直接 503：这是成本最高的入口
    （一次调用 = 数十次 LLM 摘要），管理员需要一个能立刻止血的物理开关。
    """
    require_feature("discover_refresh")
    return await _start_collect_task(
        request=request,
        user=user,
        kind="repo",
        params={"limit": limit, "force": force},
        runner=lambda state: _execute_refresh_repos(
            state,
            limit=limit,
            force=force,
            operator_user_id=user.id if user is not None else None,
        ),
    )


async def _execute_refresh_repos(
    state: task_runner.TaskState,
    *,
    limit: int,
    force: bool,
    operator_user_id: Optional[uuid.UUID] = None,
) -> None:
    """后台执行仓库抓取（进度通过 state.advance 上报）

    这里只负责「跑」和「把结果落到任务状态」；异常直接往上抛，由执行器统一
    落 failed —— 任务状态绝不允许停在 running（前端会永远转圈）。

    批次号复用任务号，所以开批次这件事必须在这里做（用 state.task_id 当 id），
    而不是在请求路径上 —— 让「立即返回」真的立即。
    """
    from src.modules.discovery.github_fetcher import GitHubFetcher, attach_readmes
    from src.modules.discovery.tech_knowledge import get_knowledge_base
    from src.services.collection_service import collect_items

    batch_id = await asyncio.to_thread(
        start_batch,
        "repo",
        trigger=BATCH_TRIGGER_MANUAL,
        operator_user_id=operator_user_id,
        params={"limit": limit, "force": force},
        batch_id=uuid.UUID(state.task_id),
    )

    fetcher = GitHubFetcher()
    try:
        try:
            kb = await asyncio.to_thread(get_knowledge_base)
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
            params={"limit": limit, "force": force},
            force=force,
            # 降级路径的卡片并没进知识库，不能标记已采集，否则知识库恢复后会被永久跳过
            persist=kb is not None,
            # 新卡的 README 原文快照：在去重之后执行，只为真正入库的卡片付费
            enrich=attach_readmes,
            batch_id=batch_id,
            on_progress=state.advance,
        )

        if kb is None:
            await asyncio.to_thread(save_feed_cache, cached)
            _apply_result(state, result)
            state.message = f"知识库不可用，本次结果暂存在 Redis（{len(cached)} 条）"
            logger.info(f"✅ 抓取完成（Redis 降级）：{len(cached)} 条")
            return

        _apply_result(state, result)
        if result.status == "empty":
            logger.info("本次抓取没有拿到新内容")
            return

        # 只查一次：原来日志和响应各查一次，等于白付一次 Qdrant 往返
        total_in_kb = await asyncio.to_thread(kb.count, "repo")
        logger.info(
            f"{'⚠️' if result.failed_count else '✅'} 抓取完成：新增 {result.new_count} 条，"
            f"跳过 {result.skipped_count} 条，失败 {result.failed_count} 条，"
            f"知识库仓库共 {total_in_kb} 条"
        )
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
        kb = await asyncio.to_thread(get_knowledge_base)
        items = await asyncio.to_thread(kb.sample, limit, item_type="article")
        total = await asyncio.to_thread(kb.count, "article")
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
    response_model=TaskAcceptedResponse,
    status_code=202,
    dependencies=[Depends(rate_limit(DISCOVER_REFRESH_IP))],
)
async def refresh_articles(
    request: Request,
    user: OptionalUser,
    # per_platform 与 time_range 都会直接放大下游成本：前者决定每个平台抓多少条、
    # 进而决定要跑多少次 LLM 摘要，后者决定平台侧的时间窗。匿名即可触发，若不给
    # 上下界，一个 `per_platform=100000` 就能把抓取配额与 LLM 额度一次性打空。
    per_platform: int = Query(10, ge=1, le=30),
    time_range: str = Query("day", pattern="^(day|week|month)$"),
    force: bool = False,
) -> Dict[str, Any]:
    """提交一次多平台技术文章抓取任务，**立即返回**（202）

    - force=True 时忽略去重，对抓到的文章全部重新摘要并覆盖写入（用于升级摘要规范/向量）
    - 与 /refresh 共用 collection_service，一次调用 = 一条采集批次 + 每张卡片一条溯源记录
    - 与 /refresh 共用同一 IP 节流桶：两者都是「触发外抓 + LLM 摘要」的成本放大面，
      分开配额等于给同一件事开两条通道
    - 与 /refresh 共用 `discover_refresh` 开关：一条通道关掉、另一条还开着，
      等于开关形同虚设
    - 与 /refresh 共用幂等锁：两条通道同时跑会重复占用同一批 LLM 配额
    """
    require_feature("discover_refresh")
    return await _start_collect_task(
        request=request,
        user=user,
        kind="article",
        params={"per_platform": per_platform, "time_range": time_range, "force": force},
        runner=lambda state: _execute_refresh_articles(
            state,
            per_platform=per_platform,
            time_range=time_range,
            force=force,
            operator_user_id=user.id if user is not None else None,
        ),
    )


async def _execute_refresh_articles(
    state: task_runner.TaskState,
    *,
    per_platform: int,
    time_range: str,
    force: bool,
    operator_user_id: Optional[uuid.UUID] = None,
) -> None:
    """后台执行文章抓取"""
    from src.modules.discovery.article_fetcher import ArticleFetcher
    from src.modules.discovery.tech_knowledge import get_knowledge_base
    from src.services.collection_service import collect_items

    batch_id = await asyncio.to_thread(
        start_batch,
        "article",
        trigger=BATCH_TRIGGER_MANUAL,
        operator_user_id=operator_user_id,
        params={"per_platform": per_platform, "time_range": time_range, "force": force},
        batch_id=uuid.UUID(state.task_id),
    )

    fetcher = ArticleFetcher()
    try:
        kb = await asyncio.to_thread(get_knowledge_base)

        result = await collect_items(
            kind="article",
            fetch=lambda: fetcher.fetch_articles(
                per_platform=per_platform, time_range=time_range
            ),
            store=kb.upsert,
            trigger=BATCH_TRIGGER_MANUAL,
            params={"per_platform": per_platform, "time_range": time_range, "force": force},
            force=force,
            batch_id=batch_id,
            on_progress=state.advance,
        )

        _apply_result(state, result)
        if result.status == "empty":
            state.message = result.message or "各平台均未返回数据，可能网络不可达"
            return

        # 同 /refresh：日志与响应共用一次查询，少一次 Qdrant 往返
        total_in_kb = await asyncio.to_thread(kb.count, "article")
        logger.info(
            f"{'⚠️' if result.failed_count else '✅'} 文章抓取完成：新增 {result.new_count} 条，"
            f"跳过 {result.skipped_count} 条，失败 {result.failed_count} 条，"
            f"知识库文章共 {total_in_kb} 条"
        )
    finally:
        await fetcher.close()


@router.get("/tasks/{task_id}", response_model=TaskProgressResponse)
async def get_collect_task(task_id: str) -> Dict[str, Any]:
    """查询采集任务的真实进度

    两段式，因为内存任务表活不过进程重启：

    1. **内存命中** → 返回实时阶段与计数（正常路径，前端每秒拉一次）
    2. **内存未命中** → 回退查 `collection_batches`。任务号就是批次号，所以重启
       后前端手里那个 id 依然有效：过程不可得，但「跑完没有、新增几条、为什么
       失败」都还在。此时 `progress_lost=true` 明确标注，不假装还在跑。

    两者都没有（id 打错 / 批次超期清理）才 404。
    """
    state = task_runner.get_task(task_id)
    if state is not None:
        return state.snapshot()

    batch = await asyncio.to_thread(get_batch, task_id)
    if batch is None:
        raise ILOException(
            "TASK_NOT_FOUND",
            "任务不存在：可能已过期，或服务重启后该批次已被清理。",
            status_code=404,
        )

    finished_ok = batch["status"] in ("succeeded", "partial")
    return {
        "task_id": task_id,
        "kind": batch["kind"],
        "status": batch["status"],
        "stage": "done" if finished_ok else "failed",
        "stage_label": "已完成" if finished_ok else "已失败",
        "processed": batch["new_count"],
        "total": batch["new_count"],
        "percent": 100,
        "new_count": batch["new_count"],
        "skipped_count": batch["skipped_count"],
        "failed_count": batch["failed_count"],
        "message": None,
        "error": batch["error"],
        "elapsed_ms": batch["elapsed_ms"],
        "started_at": batch["started_at"],
        "finished_at": batch["finished_at"],
        "progress_lost": True,
    }


# ==================== GitHub 新星榜 ====================

# 缓存键前缀：`rank:gh:` 与卡片池的 `news:feed` 分开，便于运维按前缀清理
_RANKING_KEY_PREFIX = "rank:gh"

# 抓取并缓存 50 条，再按请求的 limit 切片。
#
# 【为什么不把 limit 放进缓存键】`limit` 是调用方可自由指定的连续区间（5..50）。
# 放进缓存键 = 缓存键数量乘上 46，每条都是**一次真实的 GitHub 请求**，
# 而它们返回的其实是同一份数据的前 N 条。所以：固定抓一份最大的，按需切片。
_RANKING_FETCH_SIZE = 50


def _ranking_cache_key(window: str, language: str, *, stale: bool = False) -> str:
    """新鲜缓存与过期缓存用两个键而不是一个键 + 元数据

    原因很实际：`SETEX` 只能给一个 TTL。想「30 分钟内算新鲜、6 小时内算兜底」，
    要么自己存时间戳再判断，要么写两份。写两份之后 `GET` 就是一次取值、无解析，
    而且过期缓存在新鲜键失效后自然接管，不需要任何额外分支。
    """
    base = f"{_RANKING_KEY_PREFIX}:{window}:{language}"
    return f"{base}:stale" if stale else base


# 搜索缓存与榜单的分水岭：榜单只有「3 窗口 × 8 语言」24 个键，键可以是人可读的；
# 搜索的键由用户输入决定，数量和字符集都不可控。所以这里必须哈希 ——
# 既避免中文/空格/冒号把键的层级弄乱，也让键长恒定（用户写 64 个字符也不影响）。
_SEARCH_KEY_PREFIX = "search:gh"
_SEARCH_FETCH_SIZE = 30


def _search_cache_key(
    keyword: str, language: str, min_stars: int, sort: str, *, stale: bool = False
) -> str:
    """搜索缓存键：参数指纹 + 新鲜/过期两档

    用 sha1 前 16 位（64 bit）而不是完整摘要：碰撞概率对「15 分钟内同参数查询」
    这个量级毫无意义，而短键在 Redis 里更好排查。明文参数仍然进日志，不丢可观测性。
    """
    raw = f"{keyword}|{language}|{min_stars}|{sort}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{_SEARCH_KEY_PREFIX}:{'stale' if stale else 'fresh'}:{digest}"


def _cache_read(key: str) -> Optional[Dict[str, Any]]:
    """读缓存；Redis 不可用、键不存在、JSON 坏掉一律当作「未命中」

    榜单的正确性不依赖缓存：未命中就去抓真实数据。所以这里的任何异常都只降级、
    不上抛 —— 把「缓存坏了」变成「榜单 500」是最没有道理的失败方式。
    """
    client = get_shared_redis()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 读取榜单缓存失败 {key}: {e}")
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        # 脏数据（例如结构升级前写入的旧格式）会每次命中、每次都出错：直接删掉
        try:
            client.delete(key)
        except Exception:  # noqa: BLE001
            pass
        return None
    return data if isinstance(data, dict) else None


def _cache_write(key: str, payload: Dict[str, Any], ttl: int) -> None:
    if ttl <= 0:
        return
    client = get_shared_redis()
    if client is None:
        return
    try:
        # default=str：payload 里有 datetime，序列化成 ISO 字符串，回读时由 pydantic 解析
        client.setex(key, ttl, json.dumps(payload, ensure_ascii=False, default=str))
    except Exception as e:  # noqa: BLE001 - 写缓存失败不影响本次返回
        logger.warning(f"[WARN] 写入榜单缓存失败 {key}: {e}")


def _kb_card_to_item(card: Dict[str, Any], rank: int, now: datetime) -> Dict[str, Any]:
    """知识库卡片 -> 榜单/搜索行

    两个兜底共用同一套字段映射：它们对外是同一个 `RankingItem` 模型，
    各写一份迟早会出现「榜单有 summary_cn、搜索没有」这类不一致。
    `repo_id` 从卡片 id（`gh-<repo_id>`）反解，解不出来就填 0 —— 这个字段只用于
    展示溯源，`card_id` 才是真正的身份。
    """
    card_id = str(card.get("id") or "")
    repo_id = int(card_id[3:]) if card_id.startswith("gh-") and card_id[3:].isdigit() else 0
    stars = int(card.get("stars") or 0)
    created = card.get("published_at") or card.get("source_updated_at")
    created_dt = None
    if isinstance(created, str):
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            created_dt = None
    age_hours = None
    stars_per_day = None
    if created_dt is not None:
        age_hours = max(1.0, (now - created_dt).total_seconds() / 3600)
        stars_per_day = round(stars / (age_hours / 24), 1)
    return {
        "rank": rank,
        "repo_id": repo_id,
        "full_name": card.get("title") or card_id,
        "url": card.get("link") or "",
        "description": card.get("raw_description") or card.get("summary"),
        "summary_cn": card.get("summary") or card.get("one_liner"),
        "stars": stars,
        "forks": 0,
        "language": card.get("language"),
        "topics": list(card.get("core_concepts") or [])[:4],
        "created_at": created_dt,
        "pushed_at": None,
        "age_hours": round(age_hours, 1) if age_hours is not None else None,
        "stars_per_day": stars_per_day,
        "card_id": card_id,
        "has_explanation": True,
    }


def _kb_ranking(limit: int) -> List[Dict[str, Any]]:
    """知识库快照榜：最后一级兜底（GitHub 不可达 + 本地无缓存）

    只捞已解读过的仓库并按星数排。榜单的语义是「最火的」，本地按星排是合理近似。
    """
    from src.modules.discovery.tech_knowledge import get_knowledge_base

    kb = get_knowledge_base()
    cards = [c for c in kb.all_cards() if c.get("type") == "repo" and c.get("stars")]
    cards.sort(key=lambda c: int(c.get("stars") or 0), reverse=True)

    now = datetime.now(timezone.utc)
    return [
        _kb_card_to_item(card, rank, now)
        for rank, card in enumerate(cards[:limit], start=1)
    ]


def _kb_search(keyword: str, limit: int) -> List[Dict[str, Any]]:
    """知识库关键词兜底：**必须真的命中**，不能拿全库按星排充数

    这是搜索兜底与榜单兜底最重要的一处不同。榜单问「最火的是什么」，本地按星排
    是合理近似；搜索问「和我这个词相关的是什么」，把全库按星排一遍当结果，
    等于用一个语义不同的答案冒充用户要的答案 —— 用户只会以为「搜出来就这些」。

    tokens 之间是 AND（与 GitHub 用空格分隔多词的行为一致）。
    不做分词：中文分词要引入额外依赖，demo 量级的子串匹配已经够用。
    """
    from src.modules.discovery.tech_knowledge import get_knowledge_base

    tokens = [token.lower() for token in keyword.split() if token]
    if not tokens:
        return []

    kb = get_knowledge_base()
    now = datetime.now(timezone.utc)
    matched: List[Dict[str, Any]] = []
    for card in kb.all_cards():
        if card.get("type") != "repo":
            continue
        haystack = " ".join(
            str(card.get(key) or "")
            for key in ("title", "raw_description", "summary", "one_liner", "language")
        )
        haystack += " " + " ".join(
            str(topic) for topic in (card.get("core_concepts") or [])
        )
        haystack = haystack.lower()
        if all(token in haystack for token in tokens):
            matched.append(card)

    matched.sort(key=lambda c: int(c.get("stars") or 0), reverse=True)
    return [
        _kb_card_to_item(card, rank, now)
        for rank, card in enumerate(matched[:limit], start=1)
    ]


async def _mark_explained(items: List[Dict[str, Any]]) -> None:
    """给条目补「是否已解读」与中文摘要（原地修改，榜单与搜索共用）

    每次都现查、不吃缓存：新采集的卡片应当立刻亮起来，而不是等缓存过期 ——
    这个 join 只是一次批量查询，不贵。
    已解读的项目直接给中文摘要：榜单里一水儿英文简介，和站内其它页面完全是
    两种语言观感；而对已收录的仓库我们本来就有更好的中文文案。
    """
    if not items:
        return
    try:
        from src.services.card_service import find_cards

        known = await asyncio.to_thread(find_cards, [item["card_id"] for item in items])
    except Exception as exc:  # noqa: BLE001 - 知识库不可用不该让接口报错
        logger.warning(f"[WARN] 标注已解读状态失败: {exc}")
        return
    for item in items:
        card = known.get(item["card_id"]) or {}
        item["has_explanation"] = bool(card)
        item["summary_cn"] = card.get("summary") or card.get("one_liner") or None


@router.get(
    "/rankings",
    response_model=RankingResponse,
    dependencies=[Depends(rate_limit(RANKING_IP))],
)
async def get_github_rankings(
    window: str = Query("week", pattern="^(week|month)$", description="week | month"),
    language: str = Query("all", description="语言筛选，取值见响应里的 languages"),
    limit: int = Query(20, ge=5, le=50),
) -> Dict[str, Any]:
    """GitHub 新星榜：窗口内创建、按 star 总数排序的高星项目（只读，零 LLM 成本）

    ## 口径
    只统计**创建时间**落在窗口内（近 7 天 / 近 30 天）的仓库，按 star 总数降序。
    「本周涨了多少星」官方 API 拿不到（见 github_ranking 模块 docstring），
    所以这里是「新星榜」而不是「涨星榜」，页面上也照此措辞。

    ## 四级降级链（每一级都要能在界面上说清楚）
    1. **Redis 新鲜缓存**（30 分钟）→ `source=cache`
    2. **GitHub Search API 实时抓**（只 1 次请求）→ `source=github`，同时写新鲜 + 过期两份
    3. **过期缓存**（6 小时）→ `source=stale`、`stale=true`，并给出「可能已过期」
    4. **本机知识库快照**（按星数排已解读过的仓库）→ `source=knowledge_base`
    5. 全都没有 → `source=unavailable` + 空列表

    【为什么不做 sample 兜底】资讯卡片可以用内置示例撑场面，但榜单是**关于世界的
    事实陈述**（「这周 star 最多的项目是 X」）。编造一份看起来很像的榜单，
    比坦率地说「暂时拿不到」坏得多 —— 用户会照着假榜去学一个不存在的项目。

    【为什么语言必须是白名单】`language` 直接进缓存键，允许任意值等于给了
    一个「让缓存永不命中」的开关，几分钟就能把 GitHub Search API 的配额打空。
    """
    from src.modules.discovery.github_ranking import (
        LANGUAGE_LABELS,
        RankingUnavailable,
        fetch_ranking,
        language_options,
    )

    if language not in LANGUAGE_LABELS:
        raise ILOException(
            "RANKING_LANGUAGE_UNSUPPORTED",
            f"不支持的语言筛选：{language}",
            status_code=422,
            detail={"languages": language_options()},
        )

    fresh_key = _ranking_cache_key(window, language)
    stale_key = _ranking_cache_key(window, language, stale=True)

    payload = await asyncio.to_thread(_cache_read, fresh_key)
    source = "cache"
    note: Optional[str] = None

    if payload is None:
        try:
            payload = await fetch_ranking(
                window=window, language=language, limit=_RANKING_FETCH_SIZE
            )
            source = "github"
            payload["fetched_at"] = datetime.now(timezone.utc).isoformat()
            remaining = payload.pop("rate_remaining", None)
            # 配额余量只进日志：给用户看没有意义，但限流排查时这是唯一的线索
            logger.info(
                f"event=ranking_fetched window={window} language={language} "
                f"total={payload['total']} rate_remaining={remaining}"
            )
            await asyncio.to_thread(
                _cache_write, fresh_key, payload, settings.ranking_cache_ttl_seconds
            )
            await asyncio.to_thread(
                _cache_write, stale_key, payload, settings.ranking_stale_ttl_seconds
            )
        except RankingUnavailable as exc:
            logger.warning(f"event=ranking_unavailable window={window} reason={exc}")
            payload = await asyncio.to_thread(_cache_read, stale_key)
            if payload is not None:
                source = "stale"
                note = "GitHub 暂时不可达，这是上次成功抓取的榜单，可能已经过期。"
            else:
                snapshot = await asyncio.to_thread(_kb_ranking, limit)
                if snapshot:
                    return {
                        "window": window,
                        "language": language,
                        "window_start": None,
                        "items": snapshot,
                        "total": len(snapshot),
                        "query": None,
                        "source": "knowledge_base",
                        "stale": True,
                        "fetched_at": None,
                        "languages": language_options(),
                        "note": (
                            "GitHub 暂时不可达，且本机没有可用的榜单缓存。"
                            "下面按星数列出**本机知识库已收录**的仓库 —— 这是本地快照，不是实时排名。"
                        ),
                    }
                return {
                    "window": window,
                    "language": language,
                    "window_start": None,
                    "items": [],
                    "total": 0,
                    "query": None,
                    "source": "unavailable",
                    "stale": False,
                    "fetched_at": None,
                    "languages": language_options(),
                    "note": "GitHub 暂时不可达，本机也没有可用的缓存或知识库快照。稍后重试即可。",
                }

    # 缓存里最多 50 条，按本次请求的 limit 切片（rank 是全局名次，切片后不失真）
    items = (payload.get("items") or [])[:limit]

    # 是否「已解读过」每次都现查，不吃缓存（见 _mark_explained）
    await _mark_explained(items)

    return {
        "window": window,
        "language": language,
        "window_start": payload.get("window_start"),
        "items": items,
        "total": int(payload.get("total") or 0),
        "query": payload.get("query"),
        "source": source,
        "stale": source == "stale",
        "fetched_at": payload.get("fetched_at"),
        "languages": language_options(),
        "note": note,
    }


@router.get(
    "/search",
    response_model=SearchResponse,
    dependencies=[Depends(rate_limit(SEARCH_IP))],
)
async def search_repositories(
    q: str = Query(
        ...,
        min_length=1,
        max_length=64,
        description="关键词；支持 GitHub 限定符，如 topic:llm、language:rust",
    ),
    language: str = Query("all", description="语言筛选，取值见响应里的 languages"),
    min_stars: int = Query(0, ge=0, le=100000, description="最低 star 数"),
    sort: str = Query("stars", pattern="^(stars|updated)$", description="stars | updated"),
    limit: int = Query(15, ge=5, le=30),
) -> Dict[str, Any]:
    """按关键词搜 GitHub 仓库（只读，零 LLM 成本）

    ## 与榜单的分工
    榜单回答「最近什么最火」（预置窗口 + 排序），搜索回答「我要找什么样的仓库」。
    后者是榜单覆盖不到的长尾需求，也是这个项目「仓库查询」定位的主入口。

    ## 降级链（每一级都要能在界面上说清楚）
    1. **Redis 新鲜缓存**（15 分钟）→ `source=cache`
    2. **GitHub Search API 实时检索**（1 次请求）→ `source=github`
    3. **过期缓存**（1 小时）→ `source=stale`，提示可能已过期
    4. **本机知识库里真的命中关键词**的仓库 → `source=knowledge_base`
    5. 全都没有 → `source=unavailable` + 空列表

    【为什么兜底不能复用榜单那套】榜单问「最火的是什么」，本地按星数排是合理近似；
    搜索问「和这个词相关的是什么」，把全库按星排一遍当结果，等于用语义不同的
    答案冒充用户的答案。所以这里只返回文案里真的命中词的仓库，宁可为空。

    【为什么配额比榜单紧】榜单只有 24 个缓存键，搜索的键由用户输入决定，
    「换个词就穿透缓存」是这里独有的放大面 —— 而 GitHub Search API 未认证
    只有 10 次/分钟，两个脚本就能把全站搜索打到降级。
    """
    from src.modules.discovery.github_ranking import (
        LANGUAGE_LABELS,
        RankingUnavailable,
        SearchQueryInvalid,
        language_options,
        normalize_search_query,
        search_repos,
    )

    keyword = normalize_search_query(q)
    if not keyword:
        raise ILOException("SEARCH_KEYWORD_REQUIRED", "请输入搜索关键词", status_code=422)

    if language not in LANGUAGE_LABELS:
        raise ILOException(
            "SEARCH_LANGUAGE_UNSUPPORTED",
            f"不支持的语言筛选：{language}",
            status_code=422,
            detail={"languages": language_options()},
        )

    filters = {"language": language, "min_stars": min_stars, "sort": sort}
    fresh_key = _search_cache_key(keyword, language, min_stars, sort)
    stale_key = _search_cache_key(keyword, language, min_stars, sort, stale=True)

    payload = await asyncio.to_thread(_cache_read, fresh_key)
    source = "cache"
    note: Optional[str] = None

    if payload is None:
        try:
            payload = await search_repos(
                keyword=keyword,
                language=language,
                min_stars=min_stars,
                sort=sort,
                limit=_SEARCH_FETCH_SIZE,
            )
            source = "github"
            payload["fetched_at"] = datetime.now(timezone.utc).isoformat()
            remaining = payload.pop("rate_remaining", None)
            logger.info(
                f"event=search_fetched keyword={keyword!r} language={language} "
                f"min_stars={min_stars} total={payload['total']} rate_remaining={remaining}"
            )
            await asyncio.to_thread(
                _cache_write, fresh_key, payload, settings.search_cache_ttl_seconds
            )
            await asyncio.to_thread(
                _cache_write, stale_key, payload, settings.search_stale_ttl_seconds
            )
        except SearchQueryInvalid as exc:
            # 语法错误**不降级**：缓存和本地快照都回答不了「你的查询语法错了」，
            # 端出一份看起来正常的结果只会让用户以为「搜出来就这些」。
            raise ILOException(
                "SEARCH_QUERY_INVALID",
                "GitHub 无法解析这个检索词，试试去掉引号或改用更简单的关键词。",
                status_code=422,
                detail={"keyword": keyword},
            ) from exc
        except RankingUnavailable as exc:
            logger.warning(f"event=search_unavailable keyword={keyword!r} reason={exc}")
            payload = await asyncio.to_thread(_cache_read, stale_key)
            if payload is not None:
                source = "stale"
                note = "GitHub 暂时不可达，这是上次成功检索的结果，可能已经过期。"
            else:
                snapshot = await asyncio.to_thread(_kb_search, keyword, limit)
                if snapshot:
                    return {
                        "keyword": keyword,
                        "github_query": None,
                        "items": snapshot,
                        "total": len(snapshot),
                        "filters": filters,
                        "source": "knowledge_base",
                        "stale": True,
                        "fetched_at": None,
                        "languages": language_options(),
                        "note": (
                            "GitHub 暂时不可达，下面只列出**本机知识库里命中该关键词**的仓库，"
                            "不是 GitHub 全量搜索结果。"
                        ),
                    }
                return {
                    "keyword": keyword,
                    "github_query": None,
                    "items": [],
                    "total": 0,
                    "filters": filters,
                    "source": "unavailable",
                    "stale": False,
                    "fetched_at": None,
                    "languages": language_options(),
                    "note": "GitHub 暂时不可达，本机知识库里也没有匹配该关键词的仓库。稍后重试即可。",
                }

    # 缓存里最多 30 条，按本次请求的 limit 切片（rank 是结果序号，切片后不失真）
    items = (payload.get("items") or [])[:limit]
    await _mark_explained(items)

    return {
        "keyword": payload.get("keyword") or keyword,
        "github_query": payload.get("query"),
        "items": items,
        "total": int(payload.get("total") or 0),
        "filters": filters,
        "source": source,
        "stale": source == "stale",
        "fetched_at": payload.get("fetched_at"),
        "languages": language_options(),
        "note": note,
    }


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

    # 两个查询都是同步的（前者打 PostgreSQL，后者会走知识库即网络），
    # 且互相独立 —— 用 gather 并发卸载，比串行两次 to_thread 少一个 RTT 的等待。
    record, card = await asyncio.gather(
        asyncio.to_thread(get_provenance, card_id),
        asyncio.to_thread(find_card, card_id),
    )

    if record is None and card is None:
        raise ILOException(
            "CARD_NOT_FOUND", f"卡片不存在：{card_id}", status_code=404
        )

    # 行为溯源：只有登录用户才记，匿名浏览不落流水（隐私友好，也避免噪声）
    if user is not None:
        await log_activity_async(
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
    "/cards/{card_id}/similar",
    response_model=SimilarResponse,
    dependencies=[Depends(rate_limit(SIMILAR_IP))],
)
async def get_similar_cards(
    card_id: str,
    limit: int = Query(5, ge=1, le=12, description="返回条数（默认 5，最多 12）"),
) -> Dict[str, Any]:
    """「看了这个，接着该看什么」—— 按向量近邻召回相近的项目

    **匿名可读**：和榜单、搜索一样，这是发现链路上的一环，不该被登录墙拦住
    （问答才需要登录）。它也几乎零成本：一次 Qdrant HNSW 检索，不调 LLM。

    四个刻意的取舍：

    1. **不用候选池，走 Qdrant 原生检索**。候选池是给首页排序用的固定 500 条
       快照，超出部分永远召回不到；相似推荐要的是「全库里最像的几条」。
    2. **宁可少给，不给凑数**。低于 `similar_min_score` 的一律不返回 —— 相似
       推荐里混进不相关的项目，比一块空白更让人失望：用户点进去发现完全无关，
       就再也不会信这个区块了。
    3. **同类型召回**。仓库只找仓库：文章混进来对「接着看哪个项目」没有帮助。
    4. **不可用时不是错误**。源卡片没入库（无向量）或 Qdrant 挂了，返回
       `based_on=unavailable`，前端整块隐藏。相似推荐是增强，不该让抽屉报错。
    """
    from src.modules.discovery.tech_knowledge import get_knowledge_base
    from src.services.similar import build_similar_items

    kb = await asyncio.to_thread(get_knowledge_base)
    # 先取源卡片：既是「有没有向量」的前置判断，也提供算共享概念所需的 payload。
    # get_by_id 同样是同步网络调用，必须卸载到线程池，否则会卡住事件循环
    source = await asyncio.to_thread(kb.get_by_id, card_id)
    min_score = settings.similar_min_score

    item_type = (source or {}).get("type")
    if not item_type:
        # 卡片不在知识库里 = 没有入库向量。此时不能猜一个类型去做全文检索：
        # 拿标题当 query 重新 embed 会与库内向量不同源，结果不可信
        return {
            "card_id": card_id,
            "based_on": "unavailable",
            "reason": "no_vector",
            "min_score": min_score,
            "items": [],
        }

    # 多召回一些再装点：阈值与「概念微调」都会淘汰一部分，恰好取 limit 条
    # 会让最终结果偏少（阈值剔除后就没有替补了）
    recall = max(limit * settings.similar_recall_multiplier, limit + 4)
    result = await asyncio.to_thread(
        kb.similar_items, card_id, item_type, recall, min_score
    )

    if result.get("reason"):
        return {
            "card_id": card_id,
            "based_on": "unavailable",
            "reason": result["reason"],
            "min_score": min_score,
            "items": [],
        }

    return {
        "card_id": card_id,
        "based_on": "vector",
        "min_score": min_score,
        "items": build_similar_items(source, result.get("items") or [], limit=limit),
    }


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

    【开关关闭时为什么返回 unavailable 而不是 503】导读是「先读原文」页的辅助
    内容，原文本身（/content）不受开关影响。返回 503 会让整页报错；返回
    `origin=unavailable / reason=feature_disabled` 则让前端照常展示原文，
    只在导读区给一句「管理员已关闭」。开关的本意是**省钱**，不是把页面打坏。
    """
    if not is_enabled("card_digest"):
        return {
            "card_id": card_id,
            "digest": None,
            "origin": "unavailable",
            "reason": "feature_disabled",
        }

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
