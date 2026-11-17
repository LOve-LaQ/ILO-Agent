# Tests - GitHub 新星榜（GET /discover/rankings）
"""覆盖四层：

1. **纯函数**（查询串 / 派生指标）：不联网、不连库，离线可复现
2. **契约**：字段齐全、rank 从 1 连续、source 语义正确
3. **缓存与切片**：第二次请求命中缓存；`limit` 只切片、**不新增缓存键**
4. **降级链**：GitHub 失败 → 过期缓存 → 知识库快照 → 空列表

【为什么全程 patch 掉 fetch_ranking】榜单是唯一会真的打 GitHub 的读接口。
不 patch 的话，跑一次测试就消耗一次 Search API 配额（认证后 30 次/分钟），
CI 里还会因为外网不可达而随机红灯 —— 单元测试不能依赖别人的服务。
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from src.api.routes import discover as discover_routes
from src.modules.discovery import github_ranking as gr
from src.modules.discovery import tech_knowledge

WINDOW_KEYS = ("window", "language", "items", "total", "source", "stale", "languages")


def _repo(
    index: int,
    stars: int = 1000,
    created_hours_ago: int = 72,
    base: datetime | None = None,
) -> Dict[str, Any]:
    """构造一条 GitHub Search API 的 repository 对象（只保留用到的字段）"""
    created = (base or datetime.now(timezone.utc)) - timedelta(hours=created_hours_ago)
    return {
        "id": 1000 + index,
        "full_name": f"owner{index}/repo{index}",
        "html_url": f"https://github.com/owner{index}/repo{index}",
        "description": f"repo {index} description",
        "stargazers_count": stars,
        "forks_count": index * 3,
        "language": "Python",
        "topics": ["ai", "tools", "extra", "more", "dropped"],
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pushed_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _payload(count: int = 20, total: int = 12345) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "query": "created:>2026-11-09 archived:false",
        "window_start": "2026-11-09",
        "total": total,
        "items": [
            gr.build_item(_repo(i, stars=2000 - i * 10), i, now=now)
            for i in range(1, count + 1)
        ],
    }


@pytest.fixture
def ranking_fetch(monkeypatch):
    """把 fetch_ranking 换成可编排的假件，返回调用计数"""

    calls: List[Dict[str, Any]] = []
    state: Dict[str, Any] = {"payload": _payload(), "error": None}

    async def _fake(window: str, language: str = "all", limit: int = 20):
        calls.append({"window": window, "language": language, "limit": limit})
        if state["error"] is not None:
            raise state["error"]
        return state["payload"]

    monkeypatch.setattr(gr, "fetch_ranking", _fake)
    state["calls"] = calls
    return state


# ================================================================ 纯函数
def test_build_query_uses_window_and_archived_filter():
    query = gr.build_query("week", "all")
    assert query.startswith("created:>")
    # 归档仓库不会再更新，出现在「值得学的新项目」榜里等于把用户导向死仓库
    assert "archived:false" in query
    assert "language:" not in query


def test_build_query_quotes_language_display_name():
    """GitHub 的 language 限定符吃的是展示名，`C++` 必须加引号才不会把 + 当运算符"""
    assert 'language:"C++"' in gr.build_query("month", "cpp")
    assert 'language:"TypeScript"' in gr.build_query("week", "typescript")


def test_window_start_is_iso_date_of_window():
    today = datetime.now(timezone.utc).date()
    expected = (today - timedelta(days=30)).isoformat()
    assert gr.window_start("month") == expected


def test_build_item_uses_hours_for_star_rate():
    """派生热度必须按小时折算：5 小时拿 800 星的项目不是「800 星/天」"""
    now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    repo = _repo(1, stars=300, created_hours_ago=6, base=now)
    item = gr.build_item(repo, 1, now=now)
    assert item["age_hours"] == 6.0
    assert item["stars_per_day"] == 1200.0  # 300 星 / 0.25 天


def test_build_item_marks_card_id_and_trims_topics():
    item = gr.build_item(_repo(7), 7)
    assert item["card_id"] == "gh-1007"  # 与 github_fetcher 的卡片 id 同构
    assert item["topics"] == ["ai", "tools", "extra", "more"]  # 截到 4 个
    assert item["rank"] == 7


# ================================================================ 契约
def test_rankings_returns_contract(client, fake_redis, ranking_fetch):
    resp = client.get("/api/v1/discover/rankings?window=week&limit=10")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert set(WINDOW_KEYS) <= set(body)
    assert body["source"] == "github"
    assert body["stale"] is False
    assert body["total"] == 12345
    assert [item["rank"] for item in body["items"]] == list(range(1, 11))
    assert len(body["languages"]) >= 2
    # 语言列表里必须有 all，前端默认值依赖它
    assert body["languages"][0]["value"] == "all"


def test_rankings_slices_items_without_new_cache_keys(client, fake_redis, ranking_fetch):
    """`limit` 只切片：缓存里固定 50 条，请求多少条取多少条

    如果 limit 进了缓存键，5..50 的每个取值都会各打一次 GitHub —— 同一份数据的前 N 条。
    """
    first = client.get("/api/v1/discover/rankings?window=week&limit=7")
    assert len(first.json()["items"]) == 7
    assert ranking_fetch["calls"][0]["limit"] == discover_routes._RANKING_FETCH_SIZE

    second = client.get("/api/v1/discover/rankings?window=week&limit=20")
    assert len(second.json()["items"]) == 20
    # 第二次没有新增调用：说明缓存键与 limit 无关
    assert len(ranking_fetch["calls"]) == 1


def test_second_request_hits_cache(client, fake_redis, ranking_fetch):
    client.get("/api/v1/discover/rankings?window=week")
    second = client.get("/api/v1/discover/rankings?window=week")
    assert second.json()["source"] == "cache"
    assert len(ranking_fetch["calls"]) == 1


def test_cache_key_separates_window_and_language(client, fake_redis, ranking_fetch):
    """窗口/语言不同必须各自请求一次：混用会把 Python 榜当成全语言榜返回"""
    client.get("/api/v1/discover/rankings?window=week&language=all")
    client.get("/api/v1/discover/rankings?window=month&language=all")
    client.get("/api/v1/discover/rankings?window=week&language=python")
    assert len(ranking_fetch["calls"]) == 3


def test_explained_flag_and_chinese_summary(client, fake_redis, ranking_fetch, monkeypatch):
    """已解读的仓库要把中文摘要带出来：榜单里一水儿英文简介和站内其它页面很割裂"""
    from src.services import card_service

    monkeypatch.setattr(
        card_service,
        "find_cards",
        lambda ids: {"gh-1001": {"summary": "这是一个中文摘要", "id": "gh-1001"}},
    )

    body = client.get("/api/v1/discover/rankings?window=week&limit=5").json()
    explained = [item for item in body["items"] if item["has_explanation"]]
    assert len(explained) == 1
    assert explained[0]["summary_cn"] == "这是一个中文摘要"
    assert all(item["summary_cn"] is None for item in body["items"] if not item["has_explanation"])


# ================================================================ 降级链
def test_stale_cache_served_when_github_fails(client, fake_redis, ranking_fetch):
    client.get("/api/v1/discover/rankings?window=week")  # 建立新鲜 + 过期两份缓存

    # 手工让新鲜缓存消失（模拟 30 分钟后），保留过期缓存
    fresh_key = discover_routes._ranking_cache_key("week", "all")
    assert fake_redis.delete(fresh_key) == 1

    ranking_fetch["error"] = gr.RankingUnavailable("boom")
    body = client.get("/api/v1/discover/rankings?window=week").json()

    assert body["source"] == "stale"
    assert body["stale"] is True
    assert body["note"]  # 必须告诉用户「可能已过期」
    assert len(body["items"]) > 0


def test_knowledge_base_snapshot_when_no_cache(client, fake_redis, ranking_fetch, monkeypatch):
    """GitHub 挂了、本机也没缓存 -> 用已收录仓库按星数排，并明说是本地快照"""

    class _FakeKB:
        def all_cards(self):
            return [
                {"id": "gh-501", "type": "repo", "title": "a/big", "link": "u1",
                 "stars": 9000, "summary": "中文摘要", "language": "Python"},
                {"id": "gh-502", "type": "repo", "title": "a/small", "link": "u2",
                 "stars": 100, "language": "Go"},
                {"id": "article-1", "type": "article", "title": "文章不算"},
            ]

    monkeypatch.setattr(tech_knowledge, "get_knowledge_base", lambda: _FakeKB())
    ranking_fetch["error"] = gr.RankingUnavailable("boom")

    body = client.get("/api/v1/discover/rankings?window=week&limit=5").json()

    assert body["source"] == "knowledge_base"
    assert body["stale"] is True
    assert [item["full_name"] for item in body["items"]] == ["a/big", "a/small"]
    assert body["items"][0]["rank"] == 1
    assert body["items"][0]["repo_id"] == 501  # 从 gh-<id> 反解
    assert body["items"][0]["has_explanation"] is True
    assert "不是实时排名" in body["note"]


def test_unavailable_when_everything_fails(client, fake_redis, ranking_fetch, monkeypatch):
    """全都没有数据时返回空列表而不是编一份假榜单"""

    class _EmptyKB:
        def all_cards(self):
            return []

    monkeypatch.setattr(tech_knowledge, "get_knowledge_base", lambda: _EmptyKB())
    ranking_fetch["error"] = gr.RankingUnavailable("boom")

    body = client.get("/api/v1/discover/rankings?window=week").json()
    assert body["source"] == "unavailable"
    assert body["items"] == []
    assert body["note"]


# ================================================================ 入参与缓存健壮性
def test_unknown_language_is_rejected(client, fake_redis, ranking_fetch):
    """语言必须是白名单：任意值会让缓存永不命中，几分钟就能烧光 API 配额"""
    resp = client.get("/api/v1/discover/rankings?language=ruby")
    assert resp.status_code == 422
    assert resp.json()["code"] == "RANKING_LANGUAGE_UNSUPPORTED"
    assert ranking_fetch["calls"] == []


def test_invalid_window_is_rejected(client, fake_redis, ranking_fetch):
    resp = client.get("/api/v1/discover/rankings?window=year")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_limit_out_of_range_is_rejected(client, fake_redis, ranking_fetch):
    assert client.get("/api/v1/discover/rankings?limit=1").status_code == 422
    assert client.get("/api/v1/discover/rankings?limit=500").status_code == 422


def test_corrupted_cache_is_treated_as_miss(client, fake_redis, ranking_fetch):
    """坏缓存必须被当成未命中并清掉，否则每次请求都命中同一条坏数据"""
    fake_redis.set(discover_routes._ranking_cache_key("week", "all"), "{not json")
    body = client.get("/api/v1/discover/rankings?window=week").json()
    assert body["source"] == "github"
    assert fake_redis.get(discover_routes._ranking_cache_key("week", "all"))
