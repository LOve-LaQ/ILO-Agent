# Tests - 兴趣排序内核
"""`src/services/recommend.py` 的离线单测：排序、打散、退化向量过滤。

全部用构造向量，不碰 Qdrant / 数据库 / LLM —— 排序规则本身必须能被单独验证，
否则一旦结果不对，无法判断是「规则错了」还是「数据脏了」。
"""

import pytest

from src.services.recommend import (
    cosine_similarity,
    diversify_by_category,
    is_degenerate_vector,
    normalize_score,
    rank_candidates,
)


def _candidate(item_id: str, category: str, vector):
    return {"payload": {"id": item_id, "category": category, "title": item_id}, "vector": vector}


# ---------------------------------------------------------------- 基础数学


def test_cosine_similarity_basics():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    # 模长不影响余弦（这正是余弦相对点积的意义）
    assert cosine_similarity([2.0, 0.0], [5.0, 0.0]) == pytest.approx(1.0)


def test_cosine_similarity_rejects_mismatched_dimension():
    """维度不一致返回 None：宁可跳过这一条，也不能把两个语义空间的向量算出一个数"""
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) is None
    assert cosine_similarity([], []) is None


def test_normalize_score_maps_to_unit_interval():
    assert normalize_score(1.0) == 1.0
    assert normalize_score(-1.0) == 0.0
    assert normalize_score(0.0) == 0.5


@pytest.mark.parametrize(
    "vector",
    [None, [], [0.1, 0.1, 0.1], [0.0, 0.0, 0.0]],
    ids=["None", "empty", "constant", "all-zero"],
)
def test_degenerate_vectors_are_detected(vector):
    """常量向量（写入侧 embedding 失败的降级产物）与全零/空向量都不携带语义"""
    assert is_degenerate_vector(vector) is True


def test_real_vector_is_not_degenerate():
    assert is_degenerate_vector([0.9, 0.1, -0.4]) is False


# ---------------------------------------------------------------- 排序


def test_rank_orders_by_similarity():
    profile = [1.0, 0.0]
    candidates = [
        _candidate("far", "tools", [-1.0, 0.0]),
        _candidate("near", "tools", [1.0, 0.0]),
        _candidate("mid", "tools", [0.0, 1.0]),
    ]

    ranked = rank_candidates(candidates, profile, 3, diversify=False)

    assert [item["id"] for item in ranked] == ["near", "mid", "far"]
    assert ranked[0]["recommend_score"] == 1.0
    assert ranked[1]["recommend_score"] == pytest.approx(0.5)
    assert ranked[2]["recommend_score"] == 0.0
    assert all(item["recommend_reason"] for item in ranked)


def test_rank_respects_k():
    profile = [1.0, 0.0]
    candidates = [
        _candidate(f"c{i}", "tools", [1.0, i * 0.1]) for i in range(5)
    ]
    assert len(rank_candidates(candidates, profile, 2, diversify=False)) == 2


def test_rank_filters_degenerate_candidates():
    """退化候选必须被剔除：留着它等于往结果里掺一条「和谁都很像」的噪声"""
    profile = [1.0, 0.0]
    candidates = [
        _candidate("good", "tools", [1.0, 0.0]),
        _candidate("constant", "tools", [0.1, 0.1]),
        _candidate("missing", "tools", None),
    ]

    ranked = rank_candidates(candidates, profile, 5, diversify=False)

    assert [item["id"] for item in ranked] == ["good"]


def test_rank_returns_empty_for_missing_profile():
    """没有画像（新用户）或画像退化为常量时返回空，由调用方回退随机"""
    candidates = [_candidate("a", "tools", [1.0, 0.0])]

    assert rank_candidates(candidates, None, 3) == []
    assert rank_candidates(candidates, [0.1, 0.1], 3) == []
    assert rank_candidates(candidates, [1.0, 0.0], 0) == []


# ---------------------------------------------------------------- 类别打散


def test_diversify_keeps_adjacent_categories_different():
    """同类扎堆时把不同类的顶到前面 —— 首屏三张同质化卡片比随机还差"""
    items = [
        {"id": "1", "category": "ai_ml"},
        {"id": "2", "category": "ai_ml"},
        {"id": "3", "category": "ai_ml"},
        {"id": "4", "category": "tools"},
    ]

    ordered = diversify_by_category(items)

    assert [item["id"] for item in ordered] == ["1", "4", "2", "3"]


def test_diversify_allows_same_category_when_unavoidable():
    items = [{"id": "1", "category": "ai_ml"}, {"id": "2", "category": "ai_ml"}]
    ordered = diversify_by_category(items)
    assert [item["id"] for item in ordered] == ["1", "2"]


def test_rank_applies_diversification_by_default():
    profile = [1.0, 0.0]
    # 相似度顺序：1(ai_ml) > 2(ai_ml) > 3(tools) > 4(ai_ml)
    candidates = [
        _candidate("1", "ai_ml", [1.0, 0.0]),
        _candidate("2", "ai_ml", [0.99, 0.02]),
        _candidate("3", "tools", [0.95, 0.1]),
        _candidate("4", "ai_ml", [0.9, 0.2]),
    ]

    ranked = rank_candidates(candidates, profile, 4)

    assert [item["id"] for item in ranked] == ["1", "3", "2", "4"]
