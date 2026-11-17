# Tests - 相似推荐
"""覆盖两层的三件事：

1. **装点逻辑**（`services.similar.build_similar_items`）：理由文案、共享概念、
   排序微调、丢掉点不动的条目 —— 纯函数，不需要任何服务。
2. **检索参数**（`tech_knowledge.similar_items`）：必须排除自身、按类型过滤、
   阈值下推到 Qdrant（而不是捞满再在客户端丢）、失败时降级成 unavailable。
3. **路由契约**（`GET /discover/cards/{id}/similar`）：不明文报错，源卡片没向量
   时返回 `based_on=unavailable` 而不是 404。

这些都用假件跑：真实 Qdrant 在 CI / 离线环境不可用，而这三条规则是**纯逻辑**，
用真库反而验不到（比如「阈值是否下推」看的是调用参数，不是结果）。
"""

import pytest

from src.core import circuit_breaker
from src.modules.discovery import tech_knowledge as tk
from src.services import similar as sm

SOURCE = {
    "id": "gh-1",
    "type": "repo",
    "category": "ai_ml",
    "title": "langchain",
    "core_concepts": ["LLM", "Agent", "RAG"],
}


def _neighbour(card_id="gh-2", score=0.72, **payload):
    base = {
        "id": card_id,
        "type": "repo",
        "category": "ai_ml",
        "title": f"repo-{card_id}",
        "link": f"https://github.com/{card_id}",
        "summary": "中文摘要",
        "language": "Python",
        "stars": 1234,
        "core_concepts": ["LLM", "Agent"],
    }
    base.update(payload)
    return {"payload": base, "score": score}


# ================================================================ 装点逻辑
def test_reason_mentions_shared_concepts():
    items = sm.build_similar_items(SOURCE, [_neighbour()], limit=5)
    assert len(items) == 1
    item = items[0]
    assert item["card_id"] == "gh-2"
    assert "LLM" in item["reason"] and "Agent" in item["reason"]
    assert item["shared_concepts"] == ["LLM", "Agent"]
    # 分数必须是**原始余弦**，不能被排序加分污染
    assert item["similarity"] == 0.72


def test_reason_falls_back_to_same_category_without_shared_concepts():
    items = sm.build_similar_items(
        SOURCE, [_neighbour(core_concepts=["Kubernetes"], category="ai_ml")], limit=5
    )
    assert "AI/机器学习" in items[0]["reason"]
    assert items[0]["shared_concepts"] == []


def test_reason_is_generic_when_nothing_matches():
    items = sm.build_similar_items(
        SOURCE,
        [_neighbour(core_concepts=[], category="devops")],
        limit=5,
    )
    assert items[0]["reason"] == "与当前项目语义相近"


def test_shared_concepts_break_ties_but_do_not_invert_big_gaps():
    """概念加分只调整「本来就接近」的先后，不能把差得远的顶到前面"""
    close_but_unrelated = _neighbour("gh-a", score=0.70, core_concepts=["K8s"], category="devops")
    slightly_lower_but_related = _neighbour("gh-b", score=0.68)
    far_but_same_category = _neighbour("gh-c", score=0.55)

    items = sm.build_similar_items(
        SOURCE, [close_but_unrelated, slightly_lower_but_related, far_but_same_category], limit=5
    )
    # 0.68 + 0.07（两个共享概念 0.04 + 同方向 0.03）= 0.75 > 0.70
    assert [i["card_id"] for i in items][:2] == ["gh-b", "gh-a"]
    # 0.55 + 0.02 仍然远落后，不会被顶上来
    assert items[-1]["card_id"] == "gh-c"


def test_items_without_card_id_are_dropped():
    """没有 id 就点不动（前端靠 card_id 续接学习），留着是死链"""
    broken = {"payload": {"title": "无 id 的条目"}, "score": 0.9}
    items = sm.build_similar_items(SOURCE, [broken, _neighbour()], limit=5)
    assert [i["card_id"] for i in items] == ["gh-2"]


def test_language_names_are_not_counted_as_shared_concepts():
    """共享实现语言不算「技术概念」

    实测里第一条推荐给出的理由是「共享「Python」技术概念」—— 库里绝大多数
    Python 项目都共享这个词，它说明不了任何相似性，反而让理由显得是凑的。
    """
    source = dict(SOURCE, language="Python")
    neighbour = _neighbour(core_concepts=["Python"], language="Python", category="devops")

    items = sm.build_similar_items(source, [neighbour], limit=5)

    assert items[0]["shared_concepts"] == []
    assert "Python" not in items[0]["reason"]
    # 语言仍保留在 core_concepts 里：续接学习会话时它是有效上下文
    assert items[0]["core_concepts"] == ["Python"]


def test_limit_is_enforced():
    neighbours = [_neighbour(f"gh-{i}", score=0.9 - i * 0.01) for i in range(10)]
    assert len(sm.build_similar_items(SOURCE, neighbours, limit=3)) == 3


def test_source_can_be_missing():
    """源 payload 取不到时不该整体失败，只是理由变通用"""
    items = sm.build_similar_items(None, [_neighbour()], limit=5)
    assert len(items) == 1
    assert items[0]["reason"] == "与当前项目语义相近"


# ================================================================ 检索参数
class _FakeQdrant:
    def __init__(self, points=None, raise_exc=None):
        self.points = points or []
        self.raise_exc = raise_exc
        self.calls = []

    def query_points(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc

        class _Result:
            def __init__(self, pts):
                self.points = pts

        return _Result(self.points)


class _FakePoint:
    def __init__(self, payload, score):
        self.payload = payload
        self.score = score


@pytest.fixture
def kb_with_vector(monkeypatch):
    """绕开 __init__（会连 Qdrant/Redis），只装配一条已入库向量"""
    kb = object.__new__(tk.TechKnowledgeBase)
    monkeypatch.setattr(
        kb, "get_vectors_by_ids", lambda ids, deadline=None: {"gh-1": [0.5] * tk.VECTOR_SIZE}
    )
    circuit_breaker.reset(circuit_breaker.QDRANT_READ)
    return kb


def test_similar_items_passes_filter_type_and_threshold(kb_with_vector):
    kb_with_vector.qdrant = _FakeQdrant([_FakePoint({"id": "gh-2", "title": "x"}, 0.8)])

    result = kb_with_vector.similar_items("gh-1", "repo", limit=5, min_score=0.45)

    assert result["reason"] is None
    assert result["items"] == [{"payload": {"id": "gh-2", "title": "x"}, "score": 0.8}]

    call = kb_with_vector.qdrant.calls[0]
    # 阈值必须下推给 Qdrant：捞满再在客户端丢，最终很可能只剩两条
    assert call["score_threshold"] == 0.45
    assert call["limit"] == 5
    # 同类型过滤：仓库不该召回文章
    must = call["query_filter"].must or []
    assert any(getattr(cond, "key", "") == "type" for cond in must)
    # 排除自身：源卡片与自己的余弦恒为 1.0，不排除就永远占第一条
    must_not = call["query_filter"].must_not or []
    assert any(getattr(cond, "key", "") == "id" for cond in must_not)


def test_similar_items_reports_no_vector(kb_with_vector, monkeypatch):
    monkeypatch.setattr(kb_with_vector, "get_vectors_by_ids", lambda ids, deadline=None: {})
    kb_with_vector.qdrant = _FakeQdrant()

    result = kb_with_vector.similar_items("gh-1", "repo", limit=5)

    assert result == {"items": [], "reason": "no_vector"}
    assert kb_with_vector.qdrant.calls == []


def test_similar_items_degrades_on_qdrant_error(kb_with_vector):
    kb_with_vector.qdrant = _FakeQdrant(raise_exc=RuntimeError("boom"))

    result = kb_with_vector.similar_items("gh-1", "repo", limit=5)

    assert result["items"] == []
    assert result["reason"] == "unavailable"
    circuit_breaker.reset(circuit_breaker.QDRANT_READ)


def test_similar_items_short_circuits_when_circuit_open(kb_with_vector, monkeypatch):
    kb_with_vector.qdrant = _FakeQdrant()
    # 直接改 is_open 的返回，而不是攒够 record_failure 次数：
    # 阈值属于熔断器自己的策略，这里验的是「打开时不做无效请求」这个行为
    monkeypatch.setattr(circuit_breaker, "is_open", lambda scope: True)

    result = kb_with_vector.similar_items("gh-1", "repo", limit=5)

    # 熔断打开时连 query_points 都不该发出去
    assert result["reason"] == "unavailable"
    assert kb_with_vector.qdrant.calls == []


# ================================================================ 路由契约
class _FakeKB:
    def __init__(self, source, items, reason=None):
        self._source = source
        self._items = items
        self._reason = reason
        self.similar_calls = []

    def get_by_id(self, card_id):
        return self._source

    def similar_items(self, card_id, item_type, limit, min_score):
        self.similar_calls.append((card_id, item_type, limit, min_score))
        return {"items": self._items, "reason": self._reason}


def _patch_kb(monkeypatch, fake_kb):
    monkeypatch.setattr(tk, "get_knowledge_base", lambda: fake_kb)


def test_route_returns_items(client, monkeypatch):
    _patch_kb(monkeypatch, _FakeKB(SOURCE, [_neighbour()]))

    resp = client.get("/api/v1/discover/cards/gh-1/similar?limit=3")

    assert resp.status_code == 200
    body = resp.json()
    assert body["based_on"] == "vector"
    assert body["card_id"] == "gh-1"
    assert body["items"][0]["card_id"] == "gh-2"
    assert body["items"][0]["reason"]
    assert 0 <= body["items"][0]["similarity"] <= 1


def test_route_passes_type_and_recall(client, monkeypatch):
    fake = _FakeKB(SOURCE, [_neighbour()])
    _patch_kb(monkeypatch, fake)

    client.get("/api/v1/discover/cards/gh-1/similar?limit=5")

    card_id, item_type, recall, min_score = fake.similar_calls[0]
    assert card_id == "gh-1"
    # 召回按类型收窄，且多于 limit（阈值淘汰后要有替补）
    assert item_type == "repo"
    assert recall > 5
    assert min_score > 0


def test_route_hides_when_source_has_no_vector(client, monkeypatch):
    """源卡片不在知识库：不是 404，而是「这块不显示」"""
    _patch_kb(monkeypatch, _FakeKB(None, []))

    resp = client.get("/api/v1/discover/cards/gh-unknown/similar")

    assert resp.status_code == 200
    body = resp.json()
    assert body["based_on"] == "unavailable"
    assert body["reason"] == "no_vector"
    assert body["items"] == []


def test_route_returns_empty_list_when_nothing_similar(client, monkeypatch):
    """语义上没有相近项目时返回空列表 —— 绝不拿不相关结果凑数"""
    _patch_kb(monkeypatch, _FakeKB(SOURCE, []))

    body = client.get("/api/v1/discover/cards/gh-1/similar").json()

    assert body["based_on"] == "vector"
    assert body["items"] == []


def test_route_rejects_out_of_range_limit(client):
    assert client.get("/api/v1/discover/cards/gh-1/similar?limit=0").status_code == 422
    assert client.get("/api/v1/discover/cards/gh-1/similar?limit=99").status_code == 422
