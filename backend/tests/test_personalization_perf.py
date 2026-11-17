# Tests - 个性化首屏的性能相关保证
"""三个「看起来只是快一点」的改动，其实各自都带正确性约束，必须被守住：

1. **numpy 快速排序必须与纯 Python 语义等价**：排序结果、退化过滤、类别打散
   都不能因为换了实现而变。等价性一旦破了，用户看到的是「推荐不准」，
   而不是报错 —— 这种错最难被发现。
2. **候选池缓存必须能失效**：缓存里 payload 与矩阵行靠下标对齐，一旦新卡片
   进库但池没作废，就会出现「拿 A 的向量给 B 排序」的静默错推荐。
3. **agent 包必须保持惰性导入**：`import src.modules.agent` 曾经连带拉起
   langchain → transformers → torch（本机 ~15s），谁把 `__init__.py` 改回
   直接 import，首页首个请求就会重新变慢十几秒。
"""

import subprocess
import sys
from pathlib import Path

import pytest

from src.modules.discovery import tech_knowledge as tk
from src.services.recommend import rank_candidates, rank_pool

BACKEND_ROOT = Path(__file__).resolve().parents[1]

PROFILE = [1.0, 0.0, 0.0]


def _payloads(ids, category="tools"):
    return [{"id": item_id, "category": category, "title": item_id} for item_id in ids]


# ------------------------------------------------------------ numpy ≡ 纯 Python


def test_rank_pool_matches_rank_candidates():
    """同一个候选集，两条实现必须给出同样的顺序与同样的推荐分"""
    import numpy as np

    vectors = [
        [1.0, 0.0, 0.0],
        [0.9, 0.1, 0.0],
        [0.0, 1.0, 0.0],
        [-1.0, 0.0, 0.0],
    ]
    payloads = _payloads(["a", "b", "c", "d"])
    candidates = [
        {"payload": payload, "vector": vector}
        for payload, vector in zip(payloads, vectors)
    ]

    matrix = np.asarray(vectors, dtype=np.float64)
    matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)

    slow = rank_candidates(candidates, PROFILE, 4, diversify=False)
    fast = rank_pool(payloads, matrix, PROFILE, 4, diversify=False)

    assert [item["id"] for item in fast] == [item["id"] for item in slow] == ["a", "b", "c", "d"]
    assert [item["recommend_score"] for item in fast] == [
        item["recommend_score"] for item in slow
    ]
    assert [item["recommend_reason"] for item in fast] == [
        item["recommend_reason"] for item in slow
    ]


def test_rank_pool_rejects_dimension_mismatch():
    """池是用旧 embedding 模型建的（维度不同）时必须放弃，而不是硬算出一个数"""
    import numpy as np

    matrix = np.asarray([[1.0, 0.0]], dtype=np.float64)  # 2 维，画像 3 维
    assert rank_pool(_payloads(["a"]), matrix, PROFILE, 1) == []


def test_rank_pool_keeps_diversification():
    """快速通道也要做类别打散：否则首屏三张同质卡片的问题会在优化后复活"""
    import numpy as np

    payloads = [
        {"id": "1", "category": "ai_ml"},
        {"id": "2", "category": "ai_ml"},
        {"id": "3", "category": "tools"},
    ]
    rows = np.asarray([[1.0, 0.0], [0.99, 0.01], [0.98, 0.02]], dtype=np.float64)
    rows = rows / np.linalg.norm(rows, axis=1, keepdims=True)

    ranked = rank_pool(payloads, rows, [1.0, 0.0], 3)

    assert [item["id"] for item in ranked] == ["1", "3", "2"]


# ------------------------------------------------------------ 候选池缓存


def _bare_kb(monkeypatch, candidates):
    """造一个不连 Qdrant 的知识库实例：只验证缓存逻辑，不做网络 IO"""
    calls = {"scroll": 0}

    def fake_candidate_points(self, item_type=None, limit=500):
        calls["scroll"] += 1
        return candidates

    kb = object.__new__(tk.TechKnowledgeBase)
    kb._pool_cache = {}
    import threading

    kb._pool_lock = threading.Lock()
    monkeypatch.setattr(
        tk.TechKnowledgeBase, "candidate_points", fake_candidate_points, raising=True
    )
    return kb, calls


def _repo_candidates(count=3):
    # 维度必须是 VECTOR_SIZE：池会按库的维度校验，不等长的行按「无向量」丢弃。
    # 每行方差也要足够大，否则会被判定为退化向量（embedding 降级写入的 [0.1]*1536）。
    return [
        {
            "payload": {"id": f"c{i}"},
            # 只有第 0 维有值：保证 std > 0（常量行会被当成退化向量剔掉）
            "vector": [float(i + 1)] + [0.0] * (tk.VECTOR_SIZE - 1),
        }
        for i in range(count)
    ]


def test_candidate_pool_is_cached(monkeypatch):
    """第二次取池不能再碰 Qdrant —— 这正是把 450ms 摊掉的地方"""
    kb, calls = _bare_kb(monkeypatch, _repo_candidates())

    first = kb.candidate_pool(item_type="repo")
    second = kb.candidate_pool(item_type="repo")

    assert calls["scroll"] == 1
    assert first is second
    assert len(first["payloads"]) == 3
    assert first["dropped"] == 0


def test_candidate_pool_invalidated_on_write(monkeypatch):
    """upsert 之后池必须整池作废，否则新卡片要等 TTL 才可见"""
    kb, calls = _bare_kb(monkeypatch, _repo_candidates())

    kb.candidate_pool(item_type="repo")
    assert calls["scroll"] == 1

    # upsert 是异步方法且会真的写 Qdrant，这里只验证它带来的缓存副作用
    kb.invalidate_candidate_pool()

    kb.candidate_pool(item_type="repo")
    assert calls["scroll"] == 2


def test_candidate_pool_drops_degenerate_rows(monkeypatch):
    """常量向量（embedding 降级时写入的 [0.1]*1536）必须被池剔除并计数"""
    candidates = _repo_candidates(2) + [
        {"payload": {"id": "flat"}, "vector": [0.1] * tk.VECTOR_SIZE},
        {"payload": {"id": "missing"}, "vector": None},
    ]
    kb, _ = _bare_kb(monkeypatch, candidates)

    pool = kb.candidate_pool(item_type="repo")

    assert [item["id"] for item in pool["payloads"]] == ["c0", "c1"]
    assert pool["dropped"] == 2


def test_candidate_pool_ttl_zero_disables_cache(monkeypatch):
    """TTL=0 是明确的「不缓存」配置（排障用），不能被当成「立刻过期但写了缓存」"""
    from src.core.config import settings

    monkeypatch.setattr(settings, "candidate_pool_ttl_seconds", 0, raising=False)
    kb, calls = _bare_kb(monkeypatch, _repo_candidates())

    kb.candidate_pool(item_type="repo")
    kb.candidate_pool(item_type="repo")

    assert calls["scroll"] == 2


# ------------------------------------------------------------ 惰性导入


def test_agent_package_does_not_import_llm_stack():
    """`import src.modules.agent` 不许把 state_machine（langchain/torch）拉起来

    必须用子进程验证：pytest 进程里 state_machine 早被别的用例导入过了，
    在父进程里断言 sys.modules 只会得到假阴性。
    """
    code = (
        "import sys, src.modules.agent;"
        "assert 'src.modules.agent.state_machine' not in sys.modules,"
        "'agent 包把 state_machine（langchain/torch）连带导入了'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_agent_lazy_exports_still_work():
    """惰性化不能改变对外接口：包级名字仍能取到

    这里刻意只碰 `embedding_service`（~0.7s）。`LearningStateMachine` 所在的
    state_machine 会拉起 torch，为一句断言付十几秒不值当；它是否仍可导出由
    `test_agent_package_does_not_import_llm_stack` 的导入路径间接保证。
    """
    from src.modules.agent import EmbeddingService, get_embedding_service

    assert EmbeddingService is not None
    assert callable(get_embedding_service)

    import src.modules.agent as agent_pkg

    assert "LearningStateMachine" in agent_pkg.__all__


@pytest.mark.parametrize("name", ["nope", "LearningStateMachin"])
def test_agent_getattr_raises_for_unknown(name):
    import src.modules.agent as agent_pkg

    with pytest.raises(AttributeError):
        getattr(agent_pkg, name)
