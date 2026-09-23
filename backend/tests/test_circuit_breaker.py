# Tests - 下游熔断（src/core/circuit_breaker.py + 四处接入点）
"""覆盖任务书 Phase 4 验收：

1. Redis 不可用 / Redis 自身报错 → 一律视为「未熔断」（保护机制故障不阻断主流程）
2. 连续失败达阈值 → `is_open` 为真；开放期内不再发起调用
3. 开放期结束 → HALF_OPEN 只放行 1 个探测（抢令牌），其余继续降级
4. 探测成功 → CLOSED；探测失败 → 立刻回到 OPEN
5. 阈值配 0 / 负数 → 该下游不熔断（排查用开关）
6. `open_until` 键的 TTL 必须长于开放期本身，否则半开逻辑退化成「全部放行」
7. **四处接入点熔断打开时真的没有发起调用**（断言调用次数为 0）
8. summary 两处接入点把 `settings.llm_summary_timeout` 按次传下去
9. 熔断事件输出统一的结构化日志字段（event=circuit_open）

全部离线可复现：不触网、不依赖真实 Redis / LLM / Qdrant。
"""

import asyncio
import time
import types

import pytest

from src.core import circuit_breaker as cb
from src.core.config import settings


# ================================================================ 策略层
# ------------------------------------------------- Redis 不可用 / 报错
def test_is_open_false_when_redis_unavailable(monkeypatch):
    """Redis 挂了就当作没熔断：少一层保护，远好过全站不可用"""
    monkeypatch.setattr(cb, "get_redis", lambda: None)

    assert cb.is_open(cb.LLM_CHAT) is False
    # 计数与重置都必须是安全的 no-op，不能把异常抛给业务
    cb.record_failure(cb.LLM_CHAT)
    cb.record_success(cb.LLM_CHAT)
    cb.reset(cb.LLM_CHAT)


def test_is_open_false_when_redis_raises(monkeypatch):
    """保护机制自身故障不得阻断主流程"""

    class _Boom:
        def get(self, *args, **kwargs):
            raise RuntimeError("redis down")

    monkeypatch.setattr(cb, "get_redis", lambda: _Boom())
    assert cb.is_open(cb.LLM_CHAT) is False


def test_record_failure_swallows_redis_error(monkeypatch):
    class _Boom:
        def pipeline(self):
            raise RuntimeError("redis down")

    monkeypatch.setattr(cb, "get_redis", lambda: _Boom())
    cb.record_failure(cb.LLM_CHAT)  # 不该抛


# ------------------------------------------------- 达阈值 → OPEN
def test_consecutive_failures_open_circuit(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "circuit_failure_threshold", 3)

    for i in range(3):
        assert cb.is_open(cb.LLM_CHAT) is False, f"第 {i + 1} 次失败前不该已熔断"
        cb.record_failure(cb.LLM_CHAT)

    # 第 3 次失败达到阈值 → OPEN
    assert cb.is_open(cb.LLM_CHAT) is True


def test_open_period_keeps_circuit_open(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "circuit_failure_threshold", 1)
    cb.record_failure(cb.LLM_CHAT)

    # 开放期内反复探测都应该是 True，且不消耗探测令牌
    assert [cb.is_open(cb.LLM_CHAT) for _ in range(3)] == [True, True, True]


def test_threshold_non_positive_disables_breaker(fake_redis, monkeypatch):
    """配 0 / 负数 = 该下游不熔断（排查时临时放行）"""
    monkeypatch.setattr(settings, "circuit_failure_threshold", 0)
    for _ in range(10):
        cb.record_failure(cb.LLM_CHAT)
    assert cb.is_open(cb.LLM_CHAT) is False

    monkeypatch.setattr(settings, "circuit_failure_threshold", -1)
    for _ in range(10):
        cb.record_failure(cb.LLM_CHAT)
    assert cb.is_open(cb.LLM_CHAT) is False


def test_open_until_key_outlives_open_period(fake_redis, monkeypatch):
    """键的 TTL 必须长于开放期：否则开放期一过键就没了，无从判断「刚才处于 OPEN」"""
    monkeypatch.setattr(settings, "circuit_failure_threshold", 1)
    monkeypatch.setattr(settings, "circuit_open_seconds", 30)
    cb.record_failure(cb.LLM_CHAT)

    _, open_until_key, _ = cb._keys(cb.LLM_CHAT)
    ttl = fake_redis.ttl(open_until_key)
    assert 30 < ttl <= 60


# ------------------------------------------------- HALF_OPEN 半开
def _force_open_period_passed(redis, scope):
    """把 open_until 拨到过去，模拟开放期已结束（比 sleep 30s 确定得多）"""
    _, open_until_key, _ = cb._keys(scope)
    redis.set(open_until_key, str(time.time() - 1), ex=60)


def test_half_open_releases_exactly_one_probe(fake_redis, monkeypatch):
    """开放期结束的瞬间全部放行 = 定时惊群，会把刚恢复的下游再次打挂"""
    monkeypatch.setattr(settings, "circuit_failure_threshold", 2)
    cb.record_failure(cb.LLM_CHAT)
    cb.record_failure(cb.LLM_CHAT)
    _force_open_period_passed(fake_redis, cb.LLM_CHAT)

    # 第一个拿到探测令牌（False = 放行），其余继续降级
    assert [cb.is_open(cb.LLM_CHAT) for _ in range(3)] == [False, True, True]


def test_half_open_probe_success_closes_circuit(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "circuit_failure_threshold", 2)
    cb.record_failure(cb.LLM_CHAT)
    cb.record_failure(cb.LLM_CHAT)
    _force_open_period_passed(fake_redis, cb.LLM_CHAT)

    assert cb.is_open(cb.LLM_CHAT) is False  # 抢到令牌，放行探测
    cb.record_success(cb.LLM_CHAT)

    assert fake_redis.exists(*cb._keys(cb.LLM_CHAT)) == 0
    assert cb.is_open(cb.LLM_CHAT) is False  # CLOSED：全部放行


def test_half_open_probe_failure_reopens_circuit(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "circuit_failure_threshold", 2)
    cb.record_failure(cb.LLM_CHAT)
    cb.record_failure(cb.LLM_CHAT)
    _force_open_period_passed(fake_redis, cb.LLM_CHAT)

    assert cb.is_open(cb.LLM_CHAT) is False  # 抢到令牌
    cb.record_failure(cb.LLM_CHAT)           # 探测失败

    assert cb.is_open(cb.LLM_CHAT) is True   # 立刻回到 OPEN


def test_record_failure_returns_probe_token(fake_redis, monkeypatch):
    """探测失败必须把令牌放回去，否则半开之后再也没有人能探测"""
    monkeypatch.setattr(settings, "circuit_failure_threshold", 2)
    cb.record_failure(cb.LLM_CHAT)
    cb.record_failure(cb.LLM_CHAT)
    _force_open_period_passed(fake_redis, cb.LLM_CHAT)

    cb.is_open(cb.LLM_CHAT)  # 抢走令牌
    cb.record_failure(cb.LLM_CHAT)
    _force_open_period_passed(fake_redis, cb.LLM_CHAT)

    assert cb.is_open(cb.LLM_CHAT) is False  # 令牌已归还，能再次探测


def test_scopes_are_isolated(fake_redis, monkeypatch):
    """一个下游熔断不该连累另一个：chat 挂了不代表 embedding 也挂了"""
    monkeypatch.setattr(settings, "circuit_failure_threshold", 1)
    cb.record_failure(cb.LLM_CHAT)

    assert cb.is_open(cb.LLM_CHAT) is True
    assert cb.is_open(cb.LLM_SUMMARY) is False
    assert cb.is_open(cb.EMBEDDING) is False
    assert cb.is_open(cb.QDRANT_READ) is False


def test_circuit_open_emits_structured_log(fake_redis, monkeypatch):
    """观测要求：熔断事件要有统一的 event= / downstream= 字段，便于告警规则复用"""
    from loguru import logger

    monkeypatch.setattr(settings, "circuit_failure_threshold", 1)
    messages = []
    sink_id = logger.add(lambda m: messages.append(m), level="WARNING")
    try:
        cb.record_failure(cb.LLM_CHAT)
    finally:
        logger.remove(sink_id)

    assert any("event=circuit_open" in m for m in messages)
    assert any("downstream=llm_chat" in m for m in messages)


# ================================================================ 接入点
class _RecordingLLM:
    """记录每次调用参数；不触网"""

    def __init__(self, content="真实讲解内容"):
        self.calls = []
        self._content = content

    def invoke(self, *args, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(content=self._content)


def _open(scope, monkeypatch, times=1):
    """把某个下游直接顶到 OPEN"""
    monkeypatch.setattr(settings, "circuit_failure_threshold", 1)
    for _ in range(times):
        cb.record_failure(scope)
    assert cb.is_open(scope) is True


# ------------------------------------------------- 1. 讲解（LLM_CHAT）
def _engine(monkeypatch, llm):
    from src.modules.agent import state_machine as sm

    monkeypatch.setattr(sm, "llm", llm)
    monkeypatch.setattr(sm, "LLM_AVAILABLE", True, raising=False)
    return sm.ExplanationEngine({"max_tokens": 4096})


def test_explanation_skips_llm_when_circuit_open(fake_redis, monkeypatch):
    _open(cb.LLM_CHAT, monkeypatch)

    llm = _RecordingLLM()
    engine = _engine(monkeypatch, llm)
    result = asyncio.run(engine.generate("Python asyncio", {"summary": "s"}))

    assert llm.calls == []                                 # 一次都没发起
    assert isinstance(result, str) and result.strip()      # 走降级文案


def test_explanation_failures_trip_circuit(fake_redis, monkeypatch):
    """连续失败要能被计入，否则熔断永远打不开"""
    monkeypatch.setattr(settings, "circuit_failure_threshold", 2)

    class _Boom:
        def __init__(self):
            self.calls = 0

        def invoke(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError("upstream hang")

    llm = _Boom()
    engine = _engine(monkeypatch, llm)

    for _ in range(2):
        asyncio.run(engine.generate("Python asyncio", {"summary": "s"}))
    assert llm.calls == 2
    assert cb.is_open(cb.LLM_CHAT) is True

    # 熔断已打开：第 3 次不再发起调用
    asyncio.run(engine.generate("Python asyncio", {"summary": "s"}))
    assert llm.calls == 2


def test_explanation_success_resets_counter(fake_redis, monkeypatch):
    """失败若干次后成功一次，计数要清零 —— 否则零散失败会累积成假熔断"""
    monkeypatch.setattr(settings, "circuit_failure_threshold", 3)

    class _Flaky:
        def __init__(self):
            self.calls = 0

        def invoke(self, *args, **kwargs):
            self.calls += 1
            if self.calls <= 2:
                raise TimeoutError("transient")
            return types.SimpleNamespace(content="正常讲解")

    engine = _engine(monkeypatch, _Flaky())
    for _ in range(3):
        asyncio.run(engine.generate("Python asyncio", {"summary": "s"}))

    assert cb.is_open(cb.LLM_CHAT) is False
    failures_key, _, _ = cb._keys(cb.LLM_CHAT)
    assert fake_redis.exists(failures_key) == 0


# ------------------------------------------------- 2. 对话路由（LLM_CHAT）
def test_chat_route_skips_llm_when_circuit_open(client, test_user_id, fake_redis, monkeypatch):
    """端到端：熔断打开时路由直接降级，不发起 LLM 调用、也不 500"""
    from src.api.routes.learning import get_state_machine
    from src.modules.agent import state_machine as sm

    _open(cb.LLM_CHAT, monkeypatch)

    llm = _RecordingLLM()
    monkeypatch.setattr(sm, "llm", llm)
    monkeypatch.setattr(sm, "LLM_AVAILABLE", True, raising=False)

    session_id = "circuit-open-session"
    get_state_machine().active_sessions[session_id] = {
        "session_id": session_id,
        "user_id": str(test_user_id),
        "topic": "Python asyncio",
        "summary": "实验用会话",
        "core_concepts": ["asyncio"],
    }

    resp = client.post(
        "/api/v1/learning/chat",
        json={"session_id": session_id, "message": "讲讲", "conversation_history": []},
    )

    assert resp.status_code == 200
    assert llm.calls == []
    assert resp.json()["response"].strip()


# ------------------------------------------------- 3. 批量摘要（LLM_SUMMARY）
def test_summarize_batch_skips_llm_when_circuit_open(fake_redis, monkeypatch):
    from src.modules.discovery import github_fetcher as gf

    _open(cb.LLM_SUMMARY, monkeypatch)

    llm = _RecordingLLM(content="[]")
    monkeypatch.setattr("src.modules.agent.state_machine.summary_llm", llm)

    parsed = asyncio.run(gf._summarize_batch([{"title": "t", "description": "d"}], kind="repo"))

    assert parsed == []
    assert llm.calls == []


def test_summarize_batch_passes_summary_timeout(fake_redis, monkeypatch):
    """与 Phase 1 对 chat 的处理对齐：summary 也要按次传 timeout"""
    from src.modules.discovery import github_fetcher as gf

    llm = _RecordingLLM(content="[]")
    monkeypatch.setattr("src.modules.agent.state_machine.summary_llm", llm)

    asyncio.run(gf._summarize_batch([{"title": "t", "description": "d"}], kind="repo"))

    assert len(llm.calls) == 1
    assert llm.calls[0]["timeout"] == settings.llm_summary_timeout


def test_summarize_batch_failure_trips_summary_circuit(fake_redis, monkeypatch):
    from src.modules.discovery import github_fetcher as gf

    monkeypatch.setattr(settings, "circuit_failure_threshold", 2)

    class _Boom:
        def invoke(self, *args, **kwargs):
            raise TimeoutError("summary hang")

    monkeypatch.setattr("src.modules.agent.state_machine.summary_llm", _Boom())

    for _ in range(2):
        asyncio.run(gf._summarize_batch([{"title": "t"}], kind="repo"))

    assert cb.is_open(cb.LLM_SUMMARY) is True


# ------------------------------------------------- 4. 中文导读（LLM_SUMMARY）
# 必须过 `is_chinese_text` 的闸门（至少 10 个中日韩字符），否则导读会被丢弃
_ZH_DIGEST = "这个仓库演示了 asyncio 事件循环与协程调度的基本用法，适合入门阅读。"


def _digest_record():
    return types.SimpleNamespace(
        card_payload={"title": "asyncio 入门"},
        raw_description="一个关于协程的仓库",
        source_url="https://github.com/example/asyncio-demo",
    )


def test_digest_skips_llm_when_circuit_open(fake_redis, monkeypatch):
    from src.services import collection_service as cs

    _open(cb.LLM_SUMMARY, monkeypatch)

    llm = _RecordingLLM(content="这是一段中文导读。")
    monkeypatch.setattr("src.modules.agent.state_machine.summary_llm", llm)

    digest = asyncio.run(cs._generate_digest(_digest_record(), "card-1", "readme 正文"))

    assert digest is None
    assert llm.calls == []


def test_digest_passes_summary_timeout(fake_redis, monkeypatch):
    from src.services import collection_service as cs

    llm = _RecordingLLM(content=_ZH_DIGEST)
    monkeypatch.setattr("src.modules.agent.state_machine.summary_llm", llm)

    digest = asyncio.run(cs._generate_digest(_digest_record(), "card-1", "readme 正文"))

    assert digest == _ZH_DIGEST
    assert llm.calls[0]["timeout"] == settings.llm_summary_timeout


# ------------------------------------------------- 5. Embedding
def test_embedding_post_skips_when_circuit_open(fake_redis, monkeypatch):
    from src.modules.agent import embedding_service as es

    _open(cb.EMBEDDING, monkeypatch)

    class _Http:
        def __init__(self):
            self.calls = 0

        async def post(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("熔断打开时不该发起请求")

    svc = es.EmbeddingService.__new__(es.EmbeddingService)
    svc.http_client = _Http()

    with pytest.raises(cb.CircuitOpenError):
        asyncio.run(svc._post("https://example.invalid/emb", headers={}, json={}))

    assert svc.http_client.calls == 0


def test_embedding_5xx_trips_circuit_but_4xx_does_not(fake_redis, monkeypatch):
    """4xx 是「我们的请求有问题」，与下游健康度无关 —— 错 Key 不该把熔断顶在 OPEN"""
    import httpx

    from src.modules.agent import embedding_service as es

    monkeypatch.setattr(settings, "circuit_failure_threshold", 1)

    def _svc_with(status):
        class _Http:
            async def post(self, *args, **kwargs):
                return httpx.Response(status, request=httpx.Request("POST", "https://x.invalid"))

        svc = es.EmbeddingService.__new__(es.EmbeddingService)
        svc.http_client = _Http()
        return svc

    asyncio.run(_svc_with(401)._post("https://x.invalid", headers={}, json={}))
    assert cb.is_open(cb.EMBEDDING) is False
    # 4xx 既不算成功也不算失败：不该留下失败计数
    failures_key, _, _ = cb._keys(cb.EMBEDDING)
    assert fake_redis.exists(failures_key) == 0

    asyncio.run(_svc_with(503)._post("https://x.invalid", headers={}, json={}))
    assert cb.is_open(cb.EMBEDDING) is True


def test_embedding_network_error_trips_circuit(fake_redis, monkeypatch):
    import httpx

    from src.modules.agent import embedding_service as es

    monkeypatch.setattr(settings, "circuit_failure_threshold", 1)

    class _Http:
        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("connection reset")

    svc = es.EmbeddingService.__new__(es.EmbeddingService)
    svc.http_client = _Http()

    with pytest.raises(httpx.ConnectError):
        asyncio.run(svc._post("https://x.invalid", headers={}, json={}))

    assert cb.is_open(cb.EMBEDDING) is True


# ------------------------------------------------- 6. Qdrant 读
def test_qdrant_read_skips_when_circuit_open(fake_redis, monkeypatch):
    from src.modules.discovery import tech_knowledge as tk

    _open(cb.QDRANT_READ, monkeypatch)

    class _Qdrant:
        def __init__(self):
            self.calls = 0

        def scroll(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("熔断打开时不该查 Qdrant")

    kb = tk.TechKnowledgeBase.__new__(tk.TechKnowledgeBase)
    kb.qdrant = _Qdrant()

    assert kb.get_vectors_by_ids(["a", "b"]) == {}
    assert kb.qdrant.calls == 0


def test_qdrant_read_failure_trips_circuit(fake_redis, monkeypatch):
    from qdrant_client.http.exceptions import ResponseHandlingException

    from src.modules.discovery import tech_knowledge as tk

    monkeypatch.setattr(settings, "circuit_failure_threshold", 1)

    class _Qdrant:
        def scroll(self, *args, **kwargs):
            raise ResponseHandlingException("qdrant down")

    kb = tk.TechKnowledgeBase.__new__(tk.TechKnowledgeBase)
    kb.qdrant = _Qdrant()

    assert kb.get_vectors_by_ids(["a"]) == {}
    assert cb.is_open(cb.QDRANT_READ) is True
