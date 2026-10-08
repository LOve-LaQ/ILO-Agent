# Tests - 块级检索（查询侧）
"""`src/modules/rag/retriever.py` 的离线单测。

这个模块守着几条**做错了不会报错**的性质，所以每条都单独钉：

1. **查询侧必须传 `text_type="query"`** —— 传成 `"document"` 不报错，只是排序
   悄悄变差（实测 0.5792 vs 0.5464）。这是本模块最贵的一个错误：它不会在任何
   日志里留下痕迹，只会让检索质量长期偏低。
2. **熔断必须在 embedding 之前检查** —— 顺序反了不报错，只是 Qdrant 挂掉期间
   每个请求都白付一次 embedding 费用（按量计费）。
3. **Qdrant 的 timeout 必须是整数** —— 传 float 会让 `qdrant-client` 在构造请求时
   抛 `ValueError`，**请求根本发不出去**（P0 级别，见 `resilience.qdrant_timeout_seconds`）。
4. **阈值是后置过滤** —— 换成服务端 `score_threshold` 也能「跑通」，但会丢掉
   「最高分是多少」这个诊断信息。
5. **正文回填要防「索引后原文被改过」** —— 偏移量越界时**不能截断凑合**，
   那会得到一段看起来正常但实际错位的文字。

全部用例默认零真实 API 调用、零真实 Qdrant / PostgreSQL：下游一律用假对象。
"""

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from src.modules.rag import retriever as R
from src.modules.rag.retriever import (
    DEFAULT_MIN_SCORE,
    DEFAULT_TOP_K,
    MAX_TOP_K,
    ChunkHit,
    RetrievalOutcome,
    hydrate_texts,
    search,
)


# ==================== 假下游 ====================


class _FakePoint:
    def __init__(self, payload: Dict[str, Any], score: float):
        self.payload = payload
        self.score = score


class _FakeQueryResponse:
    def __init__(self, points: List[_FakePoint]):
        self.points = points


class _FakeQdrant:
    """假 Qdrant 客户端：记录每次调用的 kwargs，可配置成抛异常"""

    def __init__(self, points=None, exc: Optional[BaseException] = None):
        self._points = list(points or [])
        self._exc = exc
        self.calls: List[Dict[str, Any]] = []

    def query_points(self, **kwargs) -> _FakeQueryResponse:
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return _FakeQueryResponse(self._points)


class _FakeStore:
    def __init__(self, qdrant: _FakeQdrant):
        self.qdrant = qdrant


class _FakeEmbedder:
    """假 embedding 服务：记录 (text, text_type)，可配置成返回 None 或抛异常"""

    def __init__(self, vector=(0.5, 0.5, 0.5), exc: Optional[BaseException] = None):
        self._vector = vector
        self._exc = exc
        self.calls: List[tuple] = []

    async def generate_embedding(self, text: str, text_type: str = "document"):
        self.calls.append((text, text_type))
        if self._exc is not None:
            raise self._exc
        return list(self._vector) if self._vector else None


class _FakeBreaker:
    """假熔断器：避免用例依赖真实 Redis"""

    QDRANT_READ = "qdrant_read"

    def __init__(self, open_: bool = False):
        self._open = open_
        self.failures = 0
        self.successes = 0

    def is_open(self, scope: str) -> bool:
        return self._open

    def record_failure(self, scope: str) -> None:
        self.failures += 1

    def record_success(self, scope: str) -> None:
        self.successes += 1


def _payload(card_id="gh-1", ordinal=0, start=0, end=10, **extra) -> Dict[str, Any]:
    base = {
        "card_id": card_id,
        "ordinal": ordinal,
        "heading_path": ["项目", "安装"],
        "char_start": start,
        "char_end": end,
        "source_url": "https://example.com",
        "title": "owner/repo",
        "embedding_fingerprint": "aliyun:text-embedding-v2:1536",
    }
    base.update(extra)
    return base


@pytest.fixture
def wire(monkeypatch):
    """把 retriever 的三个下游（单例 / 熔断 / embedding）整体替换成假对象。

    返回一个可配置的小对象，用例通过它读写假下游的调用记录。
    """

    class _Wire:
        def __init__(self):
            self.qdrant = _FakeQdrant()
            self.store = _FakeStore(self.qdrant)
            self.breaker = _FakeBreaker()
            self.embedder = _FakeEmbedder()

        def install(self):
            monkeypatch.setattr(R, "get_chunk_store", lambda: self.store)
            monkeypatch.setattr(R, "circuit_breaker", self.breaker)

    w = _Wire()
    w.install()
    return w


def _run(query: str, **kwargs) -> RetrievalOutcome:
    return asyncio.run(search(query, **kwargs))


# ==================== 第 1 条：查询侧侧别 ====================


def test_query_uses_query_text_type(wire):
    """**必须传 `text_type="query"`** —— 传错不报错，只让排序悄悄变差

    非对称检索：文档侧和查询侧走不同编码器。这个错误没有任何日志会提示你，
    所以只能靠测试钉住。
    """
    wire.qdrant._points = [_FakePoint(_payload(), 0.9)]
    _run("怎么安装", embed_service=wire.embedder)
    assert len(wire.embedder.calls) == 1
    text, text_type = wire.embedder.calls[0]
    assert text == "怎么安装"
    assert text_type == "query", "查询侧传成了文档侧，检索精度会静默下降"


def test_query_is_not_rewritten(wire):
    """问题原样送进向量模型 —— 改写是独立的一步，不能偷偷加在这里"""
    wire.qdrant._points = [_FakePoint(_payload(), 0.9)]
    _run("  Rust 所有权  ", embed_service=wire.embedder)
    # 只做首尾去空白，不做任何改写 / 扩展
    assert wire.embedder.calls[0][0] == "Rust 所有权"


# ==================== 第 2 条：闸门顺序 ====================


def test_circuit_open_skips_embedding_entirely(wire):
    """熔断打开时**连 embedding 都不该发起** —— 那是白花钱

    顺序反了（先 embedding 再查熔断）也能「跑通」，只是 Qdrant 挂掉期间
    每个请求都白付一次按量计费的向量化。
    """
    wire.breaker._open = True
    outcome = _run("怎么安装", embed_service=wire.embedder)

    assert outcome.reason == R.REASON_CIRCUIT_OPEN
    assert wire.embedder.calls == [], "熔断打开时仍发起了 embedding 调用"
    assert wire.qdrant.calls == []


def test_expired_deadline_skips_everything(wire):
    """预算已耗尽：不发起任何调用，直接降级"""
    import time

    outcome = _run("怎么安装", deadline=time.monotonic() - 1.0,
                   embed_service=wire.embedder)
    assert outcome.reason == R.REASON_BUDGET_EXHAUSTED
    assert wire.embedder.calls == []
    assert wire.qdrant.calls == []


def test_empty_query_short_circuits(wire):
    for text in ("", "   ", None):
        outcome = _run(text, embed_service=wire.embedder)
        assert outcome.reason == R.REASON_EMPTY_QUERY
    assert wire.embedder.calls == []
    assert wire.qdrant.calls == []


def test_deadline_expiring_during_embedding_skips_qdrant(wire, monkeypatch):
    """embedding 期间预算耗尽 → 不再发起 Qdrant 调用

    不复查的话，预算刚好在 embedding 期间用完时，我们仍会发一次注定超时的
    Qdrant 请求，白占连接和线程。

    这里让 embedding 真的睡过预算（deadline 是按值传进去的，改局部变量没用，
    必须让真实时间流逝）。
    """

    async def _slow_embed(text, text_type="document"):
        await asyncio.sleep(0.05)  # 比下面 10ms 的预算长
        return [0.5, 0.5, 0.5]

    monkeypatch.setattr(wire.embedder, "generate_embedding", _slow_embed)

    import time

    outcome = _run(
        "怎么安装", deadline=time.monotonic() + 0.01, embed_service=wire.embedder
    )
    assert outcome.reason == R.REASON_BUDGET_EXHAUSTED
    assert wire.qdrant.calls == [], "预算已耗尽仍发起了 Qdrant 调用"


# ==================== 第 3 条：timeout 必须是整数 ====================


def test_qdrant_timeout_is_integer(wire):
    """**timeout 必须是 int** —— 传 float 会让请求根本发不出去

    `qdrant-client` 1.19 在构造请求时做 `int(str(kwargs["params"]["timeout"]))`，
    传 `4.0` 得到 `int("4.0")` → ValueError，异常发生在**发请求之前**。
    这是项目里踩过的 P0（所有向量写入静默失败），在检索路径上同样致命。
    """
    wire.qdrant._points = [_FakePoint(_payload(), 0.9)]
    _run("怎么安装", embed_service=wire.embedder)

    assert len(wire.qdrant.calls) == 1
    timeout = wire.qdrant.calls[0]["timeout"]
    assert isinstance(timeout, int) and not isinstance(timeout, bool), (
        f"timeout 必须是 int，实际是 {type(timeout).__name__}={timeout!r}"
    )
    assert timeout >= 1, "Qdrant 里 timeout=0 是「不设超时」，与「预算耗尽」相反"


def test_qdrant_search_targets_chunk_collection(wire):
    """检索必须打块集合，且不取向量（1536 维 6KB/条，结果只需要坐标和分数）"""
    wire.qdrant._points = [_FakePoint(_payload(), 0.9)]
    _run("怎么安装", embed_service=wire.embedder)

    call = wire.qdrant.calls[0]
    assert call["collection_name"] == R.COLLECTION == "card_chunks"
    assert call["with_vectors"] is False
    assert call["with_payload"] is True
    # 不能传 score_threshold：那会丢掉「最高分是多少」这个诊断信息
    assert "score_threshold" not in call


# ==================== 第 4 条：阈值后置过滤 ====================


def test_hits_below_threshold_are_rejected_but_best_score_reported(wire):
    """全被阈值挡掉时：hits 为空，但**最高分要报出来**

    否则「库里没有相关内容」和「有但不够像」在观测上分不开 —— 前者该去补语料，
    后者该调阈值或换模型。
    """
    wire.qdrant._points = [
        _FakePoint(_payload(ordinal=0), 0.31),
        _FakePoint(_payload(ordinal=1), 0.22),
    ]
    outcome = _run("今天天气", embed_service=wire.embedder)

    assert outcome.reason == R.REASON_BELOW_THRESHOLD
    assert outcome.hits == ()
    assert outcome.raw_hits == 2, "原始命中数要保留，用于区分「一条没返回」"
    assert outcome.best_score == pytest.approx(0.31)


def test_only_hits_above_threshold_are_returned(wire):
    wire.qdrant._points = [
        _FakePoint(_payload(ordinal=0), 0.71),
        _FakePoint(_payload(ordinal=1), 0.55),
        _FakePoint(_payload(ordinal=2), 0.18),  # 低于默认阈值
    ]
    outcome = _run("怎么安装", embed_service=wire.embedder)

    assert outcome.reason == R.REASON_OK
    assert [h.ordinal for h in outcome.hits] == [0, 1]
    assert outcome.best_score == pytest.approx(0.71)
    assert outcome.raw_hits == 3


def test_min_score_is_configurable(wire):
    wire.qdrant._points = [_FakePoint(_payload(), 0.55)]
    assert _run("q", min_score=0.6, embed_service=wire.embedder).reason == R.REASON_BELOW_THRESHOLD
    assert _run("q", min_score=0.5, embed_service=wire.embedder).reason == R.REASON_OK


def test_default_threshold_sits_in_the_measured_gap():
    """默认阈值必须落在实测的空带里：无答案上界 0.1990 < 阈值 < 有答案下界 0.5792

    这条断言把「阈值是从数据里挑的」这件事固化下来。改阈值时如果越界，
    测试会失败并提醒你去看新的分布。
    """
    assert 0.20 < DEFAULT_MIN_SCORE < 0.57


# ==================== 失败路径 ====================


def test_no_hits_from_qdrant(wire):
    wire.qdrant._points = []
    outcome = _run("怎么安装", embed_service=wire.embedder)
    assert outcome.reason == R.REASON_NO_HITS
    assert outcome.best_score == 0.0
    assert outcome.raw_hits == 0


def test_embedding_returning_none_is_reported(wire):
    wire.embedder._vector = None
    outcome = _run("怎么安装", embed_service=wire.embedder)
    assert outcome.reason == R.REASON_EMBEDDING_FAILED
    assert wire.qdrant.calls == []


def test_embedding_raising_does_not_propagate(wire):
    """检索是「锦上添花」，它挂掉不该让整个问答 500"""
    wire.embedder._exc = RuntimeError("embedding down")
    outcome = _run("怎么安装", embed_service=wire.embedder)
    assert outcome.reason == R.REASON_EMBEDDING_FAILED


def test_qdrant_error_trips_circuit_and_does_not_propagate(wire):
    wire.qdrant._exc = RuntimeError("qdrant down")
    outcome = _run("怎么安装", embed_service=wire.embedder)

    assert outcome.reason == R.REASON_QDRANT_ERROR
    assert wire.breaker.failures == 1, "Qdrant 失败必须记入熔断器，否则永远不会打开"
    assert wire.breaker.successes == 0


def test_success_resets_circuit(wire):
    wire.qdrant._points = [_FakePoint(_payload(), 0.9)]
    _run("怎么安装", embed_service=wire.embedder)
    assert wire.breaker.successes == 1


def test_points_without_card_id_are_dropped(wire):
    """缺 `card_id` 的命中取不到正文，留着只会占名额让调用方以为找到了"""
    wire.qdrant._points = [
        _FakePoint({"ordinal": 0, "char_start": 0, "char_end": 5}, 0.9),  # 无 card_id
        _FakePoint(_payload(ordinal=1), 0.8),
    ]
    outcome = _run("怎么安装", embed_service=wire.embedder)
    assert [h.ordinal for h in outcome.hits] == [1]


def test_missing_payload_fields_fall_back_to_defaults(wire):
    """payload 只有 card_id 时不能崩 —— 回填脚本可能来自更早的 schema"""
    wire.qdrant._points = [_FakePoint({"card_id": "gh-9"}, 0.9)]
    outcome = _run("怎么安装", embed_service=wire.embedder)
    assert outcome.hits[0].card_id == "gh-9"
    assert outcome.hits[0].heading_path == ()
    assert outcome.hits[0].char_start == 0


# ==================== top_k 夹取 ====================


def test_top_k_is_clamped_to_sane_range(wire):
    """`top_k=10000` 不能真的把整库拉回来 —— 那是把向量库当全表扫描用"""
    wire.qdrant._points = [_FakePoint(_payload(), 0.9)]

    _run("q", top_k=10_000, embed_service=wire.embedder)
    assert wire.qdrant.calls[-1]["limit"] == MAX_TOP_K

    _run("q", top_k=0, embed_service=wire.embedder)
    assert wire.qdrant.calls[-1]["limit"] == 1

    _run("q", top_k=-5, embed_service=wire.embedder)
    assert wire.qdrant.calls[-1]["limit"] == 1


def test_default_top_k_is_reasonable():
    assert 1 <= DEFAULT_TOP_K <= MAX_TOP_K


# ==================== 结果对象的语义 ====================


def test_outcome_semantics_separate_no_answer_from_breakage():
    """`is_degraded` 必须区分「确实没有资料」和「系统坏了」

    这个区分决定告警口径：`no_hits` 频繁说明语料不够（产品问题），
    `circuit_open` 频繁说明下游挂了（运维问题）。
    """
    no_answer = RetrievalOutcome(query="q", reason=R.REASON_NO_HITS)
    assert no_answer.answered is False
    assert no_answer.is_degraded is False, "「没资料」是正常结论，不是降级"

    broken = RetrievalOutcome(query="q", reason=R.REASON_CIRCUIT_OPEN)
    assert broken.answered is False
    assert broken.is_degraded is True, "下游挂了才是降级"

    ok = RetrievalOutcome(query="q", hits=(ChunkHit("c", 0, 0.9),), reason=R.REASON_OK)
    assert ok.answered is True
    assert ok.is_degraded is False


def test_outcome_default_reason_is_not_ok():
    """默认 reason 不能是 `ok` —— 一个空结果自称成功会让调用方误判"""
    assert RetrievalOutcome(query="q").reason != R.REASON_OK


# ==================== 正文回填 ====================


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, stmt):
        return _FakeResult(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _wire_pg(monkeypatch, rows):
    """把 get_session_factory 换成返回固定行的假实现"""
    monkeypatch.setattr(R, "get_session_factory", lambda: (lambda: _FakeSession(rows)))


RAW = "0123456789ABCDEFGHIJ"  # 20 字符，便于按偏移量肉眼核对


def test_hydrate_slices_by_char_span(monkeypatch):
    _wire_pg(monkeypatch, [("gh-1", RAW)])
    hits = [ChunkHit(card_id="gh-1", ordinal=0, score=0.9, char_start=3, char_end=8)]
    out = hydrate_texts(hits)
    assert out[0].text == RAW[3:8] == "34567"


def test_hydrate_preserves_order_and_length(monkeypatch):
    _wire_pg(monkeypatch, [("gh-1", RAW)])
    hits = [
        ChunkHit(card_id="gh-1", ordinal=0, score=0.9, char_start=0, char_end=2),
        ChunkHit(card_id="gh-1", ordinal=1, score=0.8, char_start=5, char_end=7),
    ]
    out = hydrate_texts(hits)
    assert [c.hit.ordinal for c in out] == [0, 1]
    assert [c.text for c in out] == ["01", "56"]


def test_hydrate_reports_missing_card(monkeypatch):
    """卡片被删了（采集记录不存在）→ 标 problem，不能崩"""
    _wire_pg(monkeypatch, [])
    out = hydrate_texts([ChunkHit(card_id="gh-gone", ordinal=0, score=0.9)])
    assert out[0].text is None
    assert out[0].problem == "card_missing"


def test_hydrate_refuses_out_of_range_span(monkeypatch):
    """**偏移量越界时不能截断凑合**

    原文被重新采集后变短了，同一对偏移量指到的就是别的文字。截断会得到一段
    「看起来正常但实际错位」的内容 —— 比取不到更危险，因为没人会怀疑它。
    """
    _wire_pg(monkeypatch, [("gh-1", RAW)])
    hits = [ChunkHit(card_id="gh-1", ordinal=0, score=0.9, char_start=5, char_end=999)]
    out = hydrate_texts(hits)
    assert out[0].text is None
    assert out[0].problem == "span_out_of_range"


def test_hydrate_rejects_inverted_or_empty_span(monkeypatch):
    _wire_pg(monkeypatch, [("gh-1", RAW)])
    for start, end in ((8, 3), (5, 5)):
        out = hydrate_texts(
            [ChunkHit(card_id="gh-1", ordinal=0, score=0.9, char_start=start, char_end=end)]
        )
        assert out[0].text is None
        assert out[0].problem == "span_out_of_range"


def test_hydrate_can_truncate_each_chunk(monkeypatch):
    """提示词层要控总量时按块截断 —— 截的是**尾部**，块开头信息密度最高"""
    _wire_pg(monkeypatch, [("gh-1", RAW)])
    hits = [ChunkHit(card_id="gh-1", ordinal=0, score=0.9, char_start=0, char_end=20)]
    out = hydrate_texts(hits, max_chars_per_hit=5)
    assert out[0].text == "01234"


def test_hydrate_degrades_when_postgres_is_down(monkeypatch):
    """PG 挂了 → 整批标 problem，不抛异常"""

    def _boom():
        raise RuntimeError("pg down")

    monkeypatch.setattr(R, "get_session_factory", _boom)
    out = hydrate_texts([ChunkHit(card_id="gh-1", ordinal=0, score=0.9, char_start=0, char_end=2)])
    assert out[0].text is None
    assert out[0].problem == "postgres_unavailable"


def test_hydrate_empty_input_does_not_touch_postgres(monkeypatch):
    def _boom():
        raise AssertionError("空输入不该去连数据库")

    monkeypatch.setattr(R, "get_session_factory", _boom)
    assert hydrate_texts([]) == []
