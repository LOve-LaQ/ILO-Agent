# GitHubRanking - GitHub 新星榜（官方 Search API）
"""按「周 / 月」抓 GitHub 上窗口内创建、按 star 总数排序的高星项目。

## 口径必须说明白（界面上也照抄这句）
只统计**创建时间**落在窗口内的仓库，按 star **总数**降序。这是 GitHub 官方
Search API 唯一能给出的「窗口内排序」能力。

「本周涨了多少星」官方 API **不提供** —— github.com/trending 上那句
`1,234 stars this week` 是页面自己算的，没有对应端点；官方能拿到 star 时间
的只有 `GET /repos/{o}/{r}/stargazers`（`Accept: application/vnd.github.star+json`），
要按 100 星一页翻到最后才能反推近期增量，一个 5 万星的项目就是 500 次请求。
所以这里不提供涨星数：**宁可少一个指标，也不要给一个算不准的数**。
首页的措辞统一是「新星榜」而不是「涨星榜」，就是为了不让人误读。

## 为什么不并进 GitHubFetcher
抓取器负责「采集入库」：LLM 摘要、去重、批次、README 快照，一次刷新就是几十次
LLM 调用。榜单是**只读、零 LLM 成本、可长时间缓存**的展示数据。两者塞进一个类，
「刷新一次花了多少钱」这个问题就再也回答不清了。

## 为什么语言是白名单
`language` 直接参与缓存键。若允许任意字符串，`?language=<随机值>` 就能让缓存
永远不命中、每次请求都打一次 GitHub Search API，几分钟内把 5000/h 的配额耗光，
而且顺手把我们变成别人的免费代理。白名单把「可能的缓存键数量」锁死成
`3 窗口 × 8 语言 = 24` 个，配额就烧不完。
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src.core.config import settings
from src.core.resilience import (
    is_retryable,
    raise_if_transient,
    with_retry,
)
from src.modules.discovery.github_fetcher import GITHUB_SEARCH_URL, github_headers

# 窗口 -> 天数。周/月是首页榜单的两个口径，不做任意天数：缓存键与文案都会随之膨胀
RANKING_WINDOWS: Dict[str, int] = {"week": 7, "month": 30}

# (参数值, GitHub 语言名)。GitHub 的 language 检索限定符吃的是**展示名**
# （`language:"C++"` 而不是 `language:cpp`），所以第二列同时是查询值。
LANGUAGES: Tuple[Tuple[str, str], ...] = (
    ("all", "全部"),
    ("python", "Python"),
    ("typescript", "TypeScript"),
    ("javascript", "JavaScript"),
    ("rust", "Rust"),
    ("go", "Go"),
    ("java", "Java"),
    ("cpp", "C++"),
)

LANGUAGE_LABELS: Dict[str, str] = dict(LANGUAGES)

# 语言排序在榜单里只有这几种，前端不自己维护列表（唯一事实源在后端）
def language_options() -> List[Dict[str, str]]:
    return [{"value": value, "label": label} for value, label in LANGUAGES]


class RankingUnavailable(Exception):
    """GitHub 侧拿不到数据（限流 / 5xx / 网络异常）

    单独一个异常类型：路由要按住它**走降级链**（过期缓存 -> 知识库快照），
    而不是把 502 抛给用户。混在通用 Exception 里会被错误地当成 500。
    """


class SearchQueryInvalid(Exception):
    """检索串被 GitHub 判为语法错误（422）

    与 RankingUnavailable 分开：那不是「服务不可用」，不该触发降级链 ——
    拿缓存或本地快照去回答一个语法错误的查询，用户只会以为「搜出来的东西不对」。
    """


# 用户检索词长度上限：GitHub 对超长 q 直接 422，而且它要进缓存键与日志
MAX_SEARCH_QUERY_LEN = 64

# 搜索排序：只暴露两种。GitHub 还支持 forks / help-wanted-issues，
# 但对「找值得看的仓库」没有意义，多一个值就多一倍缓存键
SEARCH_SORTS: Dict[str, str] = {"stars": "stars", "updated": "updated"}


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # GitHub 返回 2026-11-16T07:12:33Z；Python 3.11+ 的 fromisoformat 认这个格式
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001 - 时间解析失败不该让整条榜单挂掉
        return None


def build_query(window: str, language: str) -> str:
    """拼 GitHub Search 查询串

    `archived:false` 不是洁癖：归档仓库不会再更新，出现在「值得学的新项目」榜单里
    等于把用户导向一个死仓库。新仓库里归档的不多，但一旦出现最伤信任。
    """
    days = RANKING_WINDOWS[window]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    query = f"created:>{since} archived:false"
    label = LANGUAGE_LABELS.get(language)
    if label and language != "all":
        query += f' language:"{label}"'
    return query


def window_start(window: str) -> str:
    """窗口起点（ISO 日期），返回给前端做「统计 11-09 起创建的项目」这类说明"""
    days = RANKING_WINDOWS[window]
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def build_item(repo: Dict[str, Any], rank: int, now: Optional[datetime] = None) -> Dict[str, Any]:
    """把 GitHub 的 repository 对象压成榜单行

    只保留榜单需要的字段：Search API 单条返回 60+ 个键（含 license、owner 嵌套、
    permissions 等），原样透传进响应会让 20 条数据变成几十 KB，而前端一个都不用。
    """
    now = now or datetime.now(timezone.utc)
    stars = int(repo.get("stargazers_count") or 0)
    created = _parse_dt(repo.get("created_at"))

    age_hours: Optional[float] = None
    stars_per_day: Optional[float] = None
    if created is not None:
        # 用小时算再折算成「天」，因为窗口里最年轻的项目可能只有几个小时：
        # 直接 `(now - created).days` 会把 5 小时的 800 星算成 800 星/天（实际是 3840）
        age_hours = max(1.0, (now - created).total_seconds() / 3600)
        stars_per_day = round(stars / (age_hours / 24), 1)

    topics = [t for t in (repo.get("topics") or []) if isinstance(t, str)][:4]
    return {
        "rank": rank,
        "repo_id": int(repo.get("id") or 0),
        "full_name": repo.get("full_name") or "",
        "url": repo.get("html_url") or "",
        "description": (repo.get("description") or "").strip() or None,
        "stars": stars,
        "forks": int(repo.get("forks_count") or 0),
        "language": repo.get("language"),
        "topics": topics,
        "created_at": created,
        "pushed_at": _parse_dt(repo.get("pushed_at")),
        "age_hours": round(age_hours, 1) if age_hours is not None else None,
        "stars_per_day": stars_per_day,
        # 与 github_fetcher 的卡片 id 同构（gh-<repo_id>）：榜单可以直接反查
        # 「这个项目我们解读过没有」，而不用再存一份映射表
        "card_id": f"gh-{repo.get('id')}",
    }


async def _search_once(params: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """打一次 GitHub Search API，返回 (响应 JSON, 剩余配额)

    Raises:
        SearchQueryInvalid: 查询串被 GitHub 拒绝（422，通常是语法错误）
        RankingUnavailable: 非 200（限流 / 5xx）或重试耗尽
    """
    async with httpx.AsyncClient(
        timeout=settings.fetch_timeout, headers=github_headers()
    ) as client:

        async def _get() -> httpx.Response:
            resp = await client.get(GITHUB_SEARCH_URL, params=params)
            # 非 200 不转异常的话，重试层永远看不到 502/429，「5xx 可重试」会静默失效
            raise_if_transient(resp)
            return resp

        try:
            resp = await with_retry(
                _get,
                attempts=2,
                base_delay=1.0,
                should_retry=is_retryable,
                label="github_search",
            )
        except RankingUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一收敛成「拿不到数据」
            raise RankingUnavailable(f"GitHub Search API 请求失败: {type(exc).__name__}") from exc

    # 422 不是「GitHub 挂了」，是用户写的检索串 GitHub 解析不了（典型：未闭合的引号）。
    # 混进 RankingUnavailable 会让界面提示「GitHub 暂时不可达」，把用户输入错误
    # 说成服务故障 —— 用户只会反复重试同一个错误查询。
    if resp.status_code == 422:
        raise SearchQueryInvalid("GitHub 无法解析这个检索词")
    if resp.status_code != 200:
        raise RankingUnavailable(f"GitHub Search API 返回 {resp.status_code}")

    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise RankingUnavailable("GitHub Search API 返回了非 JSON 内容") from exc

    return data, resp.headers.get("X-RateLimit-Remaining")


async def fetch_ranking(
    window: str, language: str = "all", limit: int = 20
) -> Dict[str, Any]:
    """打一次 GitHub Search API 取窗口内的高星项目

    **只有一次网络请求**（per_page=limit，不翻页）：榜单只要前 N 名，
    翻页除了多花配额没有任何收益。

    Raises:
        RankingUnavailable: 非 200（限流 / 5xx）或重试耗尽
    """
    query = build_query(window, language)
    params = {
        "q": query,
        "sort": "stars",
        "order": "desc",
        "per_page": max(1, min(50, limit)),
        "page": 1,
    }

    data, remaining = await _search_once(params)

    now = datetime.now(timezone.utc)
    raw_items = data.get("items") or []
    items = [build_item(repo, 0, now=now) for repo in raw_items[: params["per_page"]]]

    # GitHub 只能按累计星数排，但 7 天窗口里「1 天龄 500 星」比「7 天龄 3000 星」更热：
    # 按日均涨星重排，rank 在重排之后才赋值。sort 是稳定的，同分保持 GitHub 原顺序。
    items.sort(key=lambda item: item.get("stars_per_day") or 0.0, reverse=True)
    for rank, item in enumerate(items, start=1):
        item["rank"] = rank

    return {
        "query": query,
        "window_start": window_start(window),
        "total": int(data.get("total_count") or 0),
        "items": items,
        # 配额余量写进日志而不是响应：给用户看没有意义，但运维排查限流时是唯一线索
        "rate_remaining": remaining,
    }


def normalize_search_query(raw: str) -> str:
    """归一化用户检索词：折叠空白 + 截断长度

    **故意不过滤 GitHub 限定符**（`topic:llm`、`language:rust`、`stars:>100`）：
    高级检索是特性，不是注入。真正要防的是长度 —— 超长 q 会被 GitHub 直接 422，
    而且它还要进缓存键与日志。
    """
    return " ".join((raw or "").split())[:MAX_SEARCH_QUERY_LEN]


def build_search_query(query: str, language: str = "all", min_stars: int = 0) -> str:
    """拼检索串：用户词 + 站内硬约束

    `archived:false` 与榜单同理：归档仓库不再维护，推给用户等于导向死仓库。
    用户自己在 q 里写 `archived:true` 时两个条件会 AND，结果必然为空 ——
    这是可接受的语义，不做特殊处理（也不该悄悄改写用户的查询）。
    """
    parts = [query, "archived:false"]
    label = LANGUAGE_LABELS.get(language)
    if label and language != "all":
        parts.append(f'language:"{label}"')
    if min_stars > 0:
        parts.append(f"stars:>={min_stars}")
    return " ".join(part for part in parts if part)


async def search_repos(
    keyword: str,
    language: str = "all",
    min_stars: int = 0,
    sort: str = "stars",
    limit: int = 20,
) -> Dict[str, Any]:
    """按关键词检索仓库（一次请求，不翻页）

    与榜单同构的部分：只取一页、只保留展示字段、零 LLM 成本。
    与榜单不同的是**总量不可信**：Search API 的 total_count 是估算值，
    这里只回传不做分页承诺。

    Raises:
        SearchQueryInvalid: 检索串语法错误（应回 422 给用户，而不是降级）
        RankingUnavailable: GitHub 不可达 / 限流
    """
    normalized = normalize_search_query(keyword)
    query = build_search_query(normalized, language, min_stars)
    params = {
        "q": query,
        "sort": SEARCH_SORTS.get(sort, "stars"),
        "order": "desc",
        "per_page": max(1, min(50, limit)),
        "page": 1,
    }

    data, remaining = await _search_once(params)

    now = datetime.now(timezone.utc)
    raw_items = data.get("items") or []
    items = [
        build_item(repo, rank, now=now)
        for rank, repo in enumerate(raw_items[: params["per_page"]], start=1)
    ]

    return {
        "query": query,
        "keyword": normalized,
        "total": int(data.get("total_count") or 0),
        "items": items,
        "rate_remaining": remaining,
    }


__all__ = [
    "LANGUAGES",
    "LANGUAGE_LABELS",
    "MAX_SEARCH_QUERY_LEN",
    "RANKING_WINDOWS",
    "RankingUnavailable",
    "SEARCH_SORTS",
    "SearchQueryInvalid",
    "build_item",
    "build_query",
    "build_search_query",
    "fetch_ranking",
    "language_options",
    "normalize_search_query",
    "search_repos",
    "window_start",
]
