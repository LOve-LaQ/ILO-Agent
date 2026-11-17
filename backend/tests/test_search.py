# Tests - 仓库搜索（GET /discover/search）
"""覆盖五层：

1. **纯函数**（检索词归一化 / 检索串拼接）：不联网、不连库
2. **契约**：字段齐全、rank 从 1 连续、filters 如实回显
3. **缓存与切片**：`limit` 只切片、不新增缓存键；第二次请求命中缓存
4. **降级链**：GitHub 失败 → 过期缓存 → **只返回关键词真正命中**的知识库卡片 → 空
5. **错误语义**：语法错误不降级、语言白名单、空白检索词

【为什么全程 patch search_repos】搜索与榜单同源，真实调用一次就消耗一次
GitHub Search API 配额（未认证仅 10 次/分钟）；CI 上还会因外网不可达而随机红灯。
"""

import copy
from typing import Any, Dict, List

import pytest

from src.api.routes import discover as discover_routes
from src.modules.discovery import github_ranking as gr
from src.modules.discovery import tech_knowledge

SEARCH_KEYS = ("keyword", "github_query", "items", "total", "filters", "source", "stale")


def _repo(index: int, stars: int = 500) -> Dict[str, Any]:
    return {
        "id": 2000 + index,
        "full_name": f"owner{index}/repo{index}",
        "html_url": f"https://github.com/owner{index}/repo{index}",
        "description": f"repo {index} description",
        "stargazers_count": stars,
        "forks_count": index,
        "language": "Python",
        "topics": ["llm", "rag"],
        "created_at": "2025-01-01T00:00:00Z",
        "pushed_at": "2026-11-01T00:00:00Z",
    }


def _payload(count: int = 12, total: int = 4321) -> Dict[str, Any]:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return {
        "query": "vector database archived:false",
        "keyword": "vector database",
        "total": total,
        "items": [
            gr.build_item(_repo(i, stars=1000 - i), i, now=now)
            for i in range(1, count + 1)
        ],
        "rate_remaining": "17",
    }


@pytest.fixture
def search_fetch(monkeypatch):
    """把 search_repos 换成可编排的假件，返回调用记录"""
    calls: List[Dict[str, Any]] = []
    state: Dict[str, Any] = {"payload": _payload(), "error": None}

    async def _fake(
        keyword: str,
        language: str = "all",
        min_stars: int = 0,
        sort: str = "stars",
        limit: int = 20,
    ):
        calls.append(
            {
                "keyword": keyword,
                "language": language,
                "min_stars": min_stars,
                "sort": sort,
                "limit": limit,
            }
        )
        if state["error"] is not None:
            raise state["error"]
        # 路由会原地写 fetched_at / pop rate_remaining，必须给副本
        return copy.deepcopy(state["payload"])

    monkeypatch.setattr(gr, "search_repos", _fake)
    state["calls"] = calls
    return state


# ================================================================ 纯函数
def test_normalize_collapses_whitespace():
    assert gr.normalize_search_query("  vector   database \n") == "vector database"


def test_normalize_truncates_overlong_keyword():
    """超长检索词会被 GitHub 直接 422，而且它还要进缓存键与日志"""
    long_text = "a" * 200
    assert len(gr.normalize_search_query(long_text)) == gr.MAX_SEARCH_QUERY_LEN


def test_build_search_query_keeps_user_operators():
    """用户写的 topic: / language: 是高级检索，不能被当成注入过滤掉"""
    keyword = gr.normalize_search_query("topic:llm agent")
    query = gr.build_search_query(keyword)
    assert "topic:llm" in query
    assert keyword in query
    assert "archived:false" in query


def test_build_search_query_quotes_language_and_stars():
    query = gr.build_search_query("rag", "cpp", 500)
    assert 'language:"C++"' in query
    assert "stars:>=500" in query


def test_build_search_query_omits_filters_when_default():
    query = gr.build_search_query("rag", "all", 0)
    assert "language:" not in query
    assert "stars:" not in query


# ================================================================ 契约
def test_search_contract(client, fake_redis, search_fetch):
    resp = client.get("/api/v1/discover/search?q=vector%20database&limit=5")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert set(SEARCH_KEYS) <= set(body)
    assert body["source"] == "github"
    assert body["stale"] is False
    assert body["keyword"] == "vector database"
    assert body["total"] == 4321
    assert [item["rank"] for item in body["items"]] == list(range(1, 6))
    # 筛选条件必须回显，否则用户会以为筛选项没生效
    assert body["filters"] == {"language": "all", "min_stars": 0, "sort": "stars"}
    assert body["languages"][0]["value"] == "all"


def test_filters_are_forwarded_to_github(client, fake_redis, search_fetch):
    client.get("/api/v1/discover/search?q=rag&language=rust&min_stars=200&sort=updated")
    call = search_fetch["calls"][0]
    assert call["language"] == "rust"
    assert call["min_stars"] == 200
    assert call["sort"] == "updated"
    # 固定抓一份再切片，limit 不进缓存键
    assert call["limit"] == discover_routes._SEARCH_FETCH_SIZE


# ================================================================ 缓存与切片
def test_second_request_hits_cache(client, fake_redis, search_fetch):
    client.get("/api/v1/discover/search?q=rag")
    second = client.get("/api/v1/discover/search?q=rag")
    assert second.json()["source"] == "cache"
    assert len(search_fetch["calls"]) == 1


def test_limit_slice_does_not_add_cache_key(client, fake_redis, search_fetch):
    """limit 是自由区间：进缓存键会让键数量乘上若干，每条都是一次真实请求"""
    client.get("/api/v1/discover/search?q=rag&limit=5")
    client.get("/api/v1/discover/search?q=rag&limit=10")
    assert len(search_fetch["calls"]) == 1
    assert len(client.get("/api/v1/discover/search?q=rag&limit=10").json()["items"]) == 10


def test_different_keyword_or_filter_needs_new_request(client, fake_redis, search_fetch):
    client.get("/api/v1/discover/search?q=rag")
    client.get("/api/v1/discover/search?q=agent")
    client.get("/api/v1/discover/search?q=rag&language=go")
    assert len(search_fetch["calls"]) == 3


# ================================================================ 降级链
def test_stale_cache_when_github_fails(client, fake_redis, search_fetch):
    client.get("/api/v1/discover/search?q=rag")
    fake_redis.delete(discover_routes._search_cache_key("rag", "all", 0, "stars"))

    search_fetch["error"] = gr.RankingUnavailable("boom")
    body = client.get("/api/v1/discover/search?q=rag").json()

    assert body["source"] == "stale"
    assert body["stale"] is True
    assert body["note"]
    assert len(body["items"]) > 0


def test_knowledge_base_fallback_only_returns_matching_cards(
    client, fake_redis, search_fetch, monkeypatch
):
    """搜索兜底必须真的命中关键词

    榜单兜底可以「按星数排本地已收录仓库」，搜索不行 —— 用户搜 vector，
    给他一个无关的高星项目，等于用语义不同的答案冒充答案。
    """

    class _FakeKB:
        def all_cards(self):
            return [
                {"id": "gh-701", "type": "repo", "title": "milvus/vector-db",
                 "link": "u1", "stars": 9000, "summary": "向量数据库", "language": "Go"},
                {"id": "gh-702", "type": "repo", "title": "someone/unrelated-cms",
                 "link": "u2", "stars": 20000, "summary": "内容管理系统"},
            ]

    monkeypatch.setattr(tech_knowledge, "get_knowledge_base", lambda: _FakeKB())
    search_fetch["error"] = gr.RankingUnavailable("boom")

    body = client.get("/api/v1/discover/search?q=vector").json()

    assert body["source"] == "knowledge_base"
    assert body["stale"] is True
    # 星数更高但不相干的那条必须被排除
    assert [item["full_name"] for item in body["items"]] == ["milvus/vector-db"]
    assert "不是 GitHub 全量搜索结果" in body["note"]


def test_unavailable_when_kb_has_no_match(client, fake_redis, search_fetch, monkeypatch):
    class _NoMatchKB:
        def all_cards(self):
            return [{"id": "gh-703", "type": "repo", "title": "x/y", "stars": 10}]

    monkeypatch.setattr(tech_knowledge, "get_knowledge_base", lambda: _NoMatchKB())
    search_fetch["error"] = gr.RankingUnavailable("boom")

    body = client.get("/api/v1/discover/search?q=vector").json()
    assert body["source"] == "unavailable"
    assert body["items"] == []
    assert body["note"]


# ================================================================ 错误语义
def test_query_syntax_error_is_not_degraded(client, fake_redis, search_fetch):
    """语法错误不能走降级：缓存与本地快照都回答不了「你这句话写错了」"""
    search_fetch["error"] = gr.SearchQueryInvalid("422")

    resp = client.get("/api/v1/discover/search?q=%22unclosed")
    assert resp.status_code == 422
    assert resp.json()["code"] == "SEARCH_QUERY_INVALID"


def test_unknown_language_is_rejected(client, fake_redis, search_fetch):
    resp = client.get("/api/v1/discover/search?q=rag&language=ruby")
    assert resp.status_code == 422
    assert resp.json()["code"] == "SEARCH_LANGUAGE_UNSUPPORTED"
    assert search_fetch["calls"] == []


def test_blank_keyword_is_rejected(client, fake_redis, search_fetch):
    resp = client.get("/api/v1/discover/search?q=%20%20%20")
    assert resp.status_code == 422
    assert resp.json()["code"] == "SEARCH_KEYWORD_REQUIRED"
    assert search_fetch["calls"] == []


def test_limit_and_sort_bounds(client, fake_redis, search_fetch):
    assert client.get("/api/v1/discover/search?q=rag&limit=1").status_code == 422
    assert client.get("/api/v1/discover/search?q=rag&limit=100").status_code == 422
    resp = client.get("/api/v1/discover/search?q=rag&sort=forks")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"
