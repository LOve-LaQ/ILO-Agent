# Tests - 兴趣画像构建
"""`src/services/interest_profile.py` 的离线单测。

分两层：
- **纯函数层**（聚合权重/时间衰减/加权平均）直接用构造数据断言，不碰任何外部依赖；
- **编排层**（build_user_profile）把数据库、Qdrant、指纹函数全部打补丁，验证
  「复用 vs 重算」的判定与各种降级路径 —— 这些分支一旦写错，线上表现是
  「看起来做了个性化，其实是随机」，最难发现。
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.modules.discovery import tech_knowledge
from src.services import interest_profile


def _uid() -> uuid.UUID:
    return uuid.uuid4()


class _FakeKB:
    """假的 Qdrant 知识库：只记录返回的向量，不连任何网络"""

    def __init__(self, vectors=None):
        self._vectors = vectors or {}
        self.calls = 0

    def get_vectors_by_ids(self, item_ids):
        self.calls += 1
        return {item_id: self._vectors[item_id] for item_id in item_ids if item_id in self._vectors}


# ---------------------------------------------------------------- 权重与衰减


def test_aggregate_signals_applies_weights_and_decay():
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)
    signals = [
        ("hot", 3.0, now),                                   # 收藏，刚发生
        ("hot", 1.0, now - timedelta(days=14)),              # 浏览，恰好一个半衰期前
        ("cold", 3.0, now - timedelta(days=28)),             # 收藏，两个半衰期前
    ]

    weights = interest_profile.aggregate_signals(signals, now=now, half_life_days=14)

    assert weights["hot"] == pytest.approx(3.0 + 0.5)
    assert weights["cold"] == pytest.approx(0.75)
    # 核心性质：同样的动作，越久远贡献越小
    assert weights["hot"] > weights["cold"]


def test_aggregate_signals_without_decay_keeps_raw_weights():
    """半衰期配 0 表示「关掉时间维度」，是合法配置而不是错误"""
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)
    weights = interest_profile.aggregate_signals(
        [("old", 2.0, now - timedelta(days=365))], now=now, half_life_days=0
    )
    assert weights["old"] == pytest.approx(2.0)


def test_aggregate_signals_skips_entries_without_card():
    weights = interest_profile.aggregate_signals(
        [(None, 3.0, None), ("", 1.0, None)], half_life_days=14
    )
    assert weights == {}


def test_aggregate_signals_tolerates_naive_timestamp():
    """库里的时间若为 naive，不能因为与 aware 的 now 相减而直接崩掉"""
    naive = datetime(2026, 9, 23, 12, 0, 0)
    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
    weights = interest_profile.aggregate_signals(
        [("a", 1.0, naive)], now=now, half_life_days=14
    )
    assert weights["a"] == pytest.approx(1.0)


def test_top_candidates_limits_and_is_deterministic():
    weights = {"c": 2.0, "b": 1.0, "a": 1.0}
    # 权重相同按 id 排序：同一份输入永远给出同一个 top-N，便于复现
    assert interest_profile.top_candidates(weights, 2) == ["c", "a"]
    assert interest_profile.top_candidates(weights, 0) == []
    assert interest_profile.top_candidates(weights, 10) == ["c", "a", "b"]


# ---------------------------------------------------------------- 加权平均


def test_weighted_mean_vector_respects_weights_and_normalizes():
    vector = interest_profile.weighted_mean_vector([(3.0, [1.0, 0.0]), (1.0, [0.0, 1.0])])

    # 加权平均 → [0.75, 0.25]，再 L2 归一化
    assert vector == pytest.approx([0.9486833, 0.3162278], rel=1e-6)
    assert sum(value * value for value in vector) == pytest.approx(1.0)


def test_weighted_mean_vector_drops_degenerate_inputs():
    vector = interest_profile.weighted_mean_vector(
        [(1.0, [1.0, 0.0]), (1.0, [0.1, 0.1]), (1.0, None)]
    )
    # 常量向量与缺失向量被剔除，只剩第一个
    assert vector == pytest.approx([1.0, 0.0])


def test_weighted_mean_vector_skips_mismatched_dimension():
    vector = interest_profile.weighted_mean_vector(
        [(1.0, [1.0, 0.0]), (1.0, [1.0, 0.0, 0.0])]
    )
    assert vector == pytest.approx([1.0, 0.0])


def test_weighted_mean_vector_returns_none_when_nothing_usable():
    assert interest_profile.weighted_mean_vector([]) is None
    assert interest_profile.weighted_mean_vector([(1.0, [0.1, 0.1])]) is None
    assert interest_profile.weighted_mean_vector([(0.0, [1.0, 0.0])]) is None


# ---------------------------------------------------------------- 指纹


def test_embedding_fingerprint_is_none_without_api_key(monkeypatch):
    """没有 Key 就没有确定的语义空间，此时不构建画像（返回 None 而非猜一个）"""
    for name in ("ALIYUN_API_KEY", "VOYAGE_API_KEY", "ZHIPU_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALIYUN_API_KEY", "")
    assert interest_profile.embedding_fingerprint() is None


# ---------------------------------------------------------------- 编排与降级


def _stub_env(monkeypatch, *, fingerprint="test:embedding:2", signals=None):
    monkeypatch.setattr(interest_profile, "is_database_configured", lambda: True)
    monkeypatch.setattr(interest_profile, "embedding_fingerprint", lambda: fingerprint)
    monkeypatch.setattr(
        interest_profile, "collect_signals", lambda uid: list(signals or [])
    )


def test_build_profile_returns_none_without_database(monkeypatch):
    monkeypatch.setattr(interest_profile, "is_database_configured", lambda: False)
    assert interest_profile.build_user_profile(_uid()) is None


def test_build_profile_returns_none_without_signals(monkeypatch):
    """新用户没有行为 → 无画像可建，交给随机兜底（而不是造一个假画像）"""
    _stub_env(monkeypatch, signals=[])
    assert interest_profile.build_user_profile(_uid()) is None


def test_build_profile_returns_none_for_invalid_user_id(monkeypatch):
    monkeypatch.setattr(interest_profile, "is_database_configured", lambda: True)
    assert interest_profile.build_user_profile("not-a-uuid") is None


def test_build_profile_returns_none_without_embedding_fingerprint(monkeypatch):
    _stub_env(monkeypatch, fingerprint=None)
    assert interest_profile.build_user_profile(_uid()) is None


def test_build_profile_returns_none_when_qdrant_unavailable(monkeypatch):
    _stub_env(monkeypatch, signals=[("card-a", 3.0, datetime.now(timezone.utc))])
    monkeypatch.setattr(interest_profile, "_load_stored", lambda uid: None)

    def _boom():
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(tech_knowledge, "get_knowledge_base", _boom)
    assert interest_profile.build_user_profile(_uid()) is None


def test_build_profile_does_not_persist_when_all_vectors_degenerate(monkeypatch):
    """候选全为退化向量 → 不写画像：写一个空画像会让下次误判为「已有画像」"""
    _stub_env(monkeypatch, signals=[("card-a", 3.0, datetime.now(timezone.utc))])
    monkeypatch.setattr(interest_profile, "_load_stored", lambda uid: None)
    monkeypatch.setattr(tech_knowledge, "get_knowledge_base", lambda: _FakeKB({"card-a": [0.1, 0.1]}))

    saved = []
    monkeypatch.setattr(
        interest_profile, "_save_profile", lambda *args, **kwargs: saved.append(args)
    )

    assert interest_profile.build_user_profile(_uid()) is None
    assert saved == []


def test_build_profile_computes_and_persists(monkeypatch):
    now = datetime.now(timezone.utc)
    _stub_env(
        monkeypatch,
        signals=[("card-a", 3.0, now), ("card-b", 1.0, now)],
    )
    monkeypatch.setattr(interest_profile, "_load_stored", lambda uid: None)
    kb = _FakeKB({"card-a": [1.0, 0.0], "card-b": [0.0, 1.0]})
    monkeypatch.setattr(tech_knowledge, "get_knowledge_base", lambda: kb)

    saved = {}
    monkeypatch.setattr(
        interest_profile,
        "_save_profile",
        lambda uid, vector, fingerprint, count: saved.update(
            vector=vector, fingerprint=fingerprint, count=count
        ),
    )

    profile = interest_profile.build_user_profile(_uid())

    assert profile is not None
    assert sum(value * value for value in profile) == pytest.approx(1.0)
    assert saved["fingerprint"] == "test:embedding:2"
    assert saved["count"] == 2
    assert kb.calls == 1


def test_build_profile_reuses_stored_profile_without_refetching_vectors(monkeypatch):
    """指纹与卡片数都没变 → 直接复用，不再去 Qdrant 取一遍向量"""
    now = datetime.now(timezone.utc)
    _stub_env(monkeypatch, signals=[("card-a", 3.0, now)])
    monkeypatch.setattr(
        interest_profile, "_load_stored", lambda uid: ([0.6, 0.8], "test:embedding:2", 1)
    )
    kb = _FakeKB({"card-a": [1.0, 0.0]})
    monkeypatch.setattr(tech_knowledge, "get_knowledge_base", lambda: kb)

    profile = interest_profile.build_user_profile(_uid())

    assert profile == pytest.approx([0.6, 0.8])
    assert kb.calls == 0


def test_build_profile_recomputes_when_fingerprint_changed(monkeypatch):
    """换了 embedding 模型 → 旧画像与新向量不在同一语义空间，必须重算"""
    now = datetime.now(timezone.utc)
    _stub_env(monkeypatch, signals=[("card-a", 3.0, now)])
    monkeypatch.setattr(
        interest_profile, "_load_stored", lambda uid: ([0.6, 0.8], "old:embedding:2", 1)
    )
    kb = _FakeKB({"card-a": [1.0, 0.0]})
    monkeypatch.setattr(tech_knowledge, "get_knowledge_base", lambda: kb)
    monkeypatch.setattr(interest_profile, "_save_profile", lambda *args, **kwargs: None)

    profile = interest_profile.build_user_profile(_uid())

    assert profile == pytest.approx([1.0, 0.0])
    assert kb.calls == 1


def test_build_profile_recomputes_when_candidate_count_changed(monkeypatch):
    """有新的兴趣信号进来（卡片数变了）→ 画像已落后，重算"""
    now = datetime.now(timezone.utc)
    _stub_env(
        monkeypatch,
        signals=[("card-a", 3.0, now), ("card-b", 1.0, now)],
    )
    monkeypatch.setattr(
        interest_profile, "_load_stored", lambda uid: ([0.6, 0.8], "test:embedding:2", 1)
    )
    kb = _FakeKB({"card-a": [1.0, 0.0], "card-b": [0.0, 1.0]})
    monkeypatch.setattr(tech_knowledge, "get_knowledge_base", lambda: kb)
    monkeypatch.setattr(interest_profile, "_save_profile", lambda *args, **kwargs: None)

    profile = interest_profile.build_user_profile(_uid())

    assert profile is not None
    assert kb.calls == 1


# ---------------------------------------------------------------- 信号源约束


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _RecordingSession:
    """只把语句编译成 SQL 记下来，不连数据库 —— 用于断言查询带了哪些约束"""

    def __init__(self, statements):
        self._statements = statements

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, statement):
        self._statements.append(
            str(statement.compile(compile_kwargs={"literal_binds": True}))
        )
        return _FakeResult([])


def test_collect_signals_reads_only_card_targeted_activities(monkeypatch):
    """行为流水必须按 `target_type='card'` 过滤。

    会话类动作（start_session / submit_quiz / complete_session）的 target_id 是
    sess_xxx 会话 id，而不是卡片 id。只按 action_type 过滤就会把会话 id 当成卡片
    塞进候选池：既白跑一轮向量查询，又会在 top-N 里与真实卡片抢名额。
    """
    statements: list[str] = []
    monkeypatch.setattr(
        interest_profile,
        "get_session_factory",
        lambda: (lambda: _RecordingSession(statements)),
    )

    assert interest_profile.collect_signals(_uid()) == []

    activity_sql = next(sql for sql in statements if "user_activities" in sql)
    assert "user_activities.target_type = 'card'" in activity_sql
    assert "'bookmark'" in activity_sql and "'view_card'" in activity_sql
    # 会话维度的动作不得出现在卡片查询里（它们应由 learning_sessions 表提供）
    for action in ("start_session", "submit_quiz", "complete_session"):
        assert action not in activity_sql
