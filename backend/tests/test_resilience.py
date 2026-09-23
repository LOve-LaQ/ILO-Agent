# Tests - 下游重试与退避编排层（src/core/resilience.py）
"""覆盖重试层的五类关键行为（对应任务书 Phase 2 验收）：

1. 成功首试 —— 不重试
2. 瞬时失败后成功
3. 重试耗尽 —— 抛出的是**原始异常**，不是包装异常
4. 不该重试的异常 —— 一次都不重试（4xx / 业务异常）
5. deadline 预算不足 —— 不再重试

全部离线可复现：不触网、不依赖 Redis / PostgreSQL / Qdrant。
退避时间统一用极小 base_delay，避免测试真的睡几秒。
"""

import asyncio
import time

import httpx
import pytest

from src.core import resilience


class _Flaky:
    """按脚本依次「抛异常 / 返回结果」，并记录调用次数。

    最后一个结果会被重复使用，因此 `_Flaky(httpx.ConnectError("x"))` 表示「一直失败」。
    """

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def _next(self):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def __call__(self):
        return self._next()

    async def acall(self):
        return self._next()


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.invalid/x")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


# ---------------------------------------------------------------- 1. 成功首试
def test_first_try_success_does_not_retry():
    flaky = _Flaky("ok")
    result = asyncio.run(resilience.with_retry(
        flaky.acall, attempts=2, base_delay=0.01,
        retry_on=resilience.RETRY_ON_NETWORK, label="test",
    ))
    assert result == "ok"
    assert flaky.calls == 1


# ---------------------------------------------------------------- 2. 瞬时失败后成功
def test_transient_failure_then_success():
    flaky = _Flaky(httpx.ConnectError("boom"), "ok")
    result = asyncio.run(resilience.with_retry(
        flaky.acall, attempts=2, base_delay=0.01,
        retry_on=resilience.RETRY_ON_NETWORK, label="test",
    ))
    assert result == "ok"
    assert flaky.calls == 2


# ---------------------------------------------------------------- 3. 重试耗尽
def test_retry_exhausted_raises_original_exception():
    original = httpx.ReadTimeout("slow")
    flaky = _Flaky(original)
    with pytest.raises(httpx.ReadTimeout) as excinfo:
        asyncio.run(resilience.with_retry(
            flaky.acall, attempts=2, base_delay=0.01,
            retry_on=resilience.RETRY_ON_NETWORK, label="test",
        ))
    # 必须原样抛出：包装成新异常会丢掉下游的真实错误类型，降级判定会失准
    assert excinfo.value is original
    assert flaky.calls == 3  # 首次 + 2 次重试


# ---------------------------------------------------------------- 4. 不该重试
def test_business_exception_is_not_retried():
    flaky = _Flaky(ValueError("bad input"))
    with pytest.raises(ValueError):
        asyncio.run(resilience.with_retry(
            flaky.acall, attempts=3, base_delay=0.01,
            retry_on=resilience.RETRY_ON_NETWORK, label="test",
        ))
    assert flaky.calls == 1


def test_exception_outside_retry_on_whitelist_is_not_retried():
    flaky = _Flaky(RuntimeError("nope"))
    with pytest.raises(RuntimeError):
        asyncio.run(resilience.with_retry(
            flaky.acall, attempts=3, base_delay=0.01,
            retry_on=(httpx.TransportError,), label="test",
        ))
    assert flaky.calls == 1


def test_http_404_is_not_retried():
    """4xx 是「Key 错了 / 参数错了」，重试一万次结果一样"""
    flaky = _Flaky(_status_error(404))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(resilience.with_retry(
            flaky.acall, attempts=3, base_delay=0.01,
            should_retry=resilience.is_retryable, label="test",
        ))
    assert flaky.calls == 1


def test_http_503_is_retried():
    flaky = _Flaky(_status_error(503), "ok")
    result = asyncio.run(resilience.with_retry(
        flaky.acall, attempts=2, base_delay=0.01,
        should_retry=resilience.is_retryable, label="test",
    ))
    assert result == "ok"
    assert flaky.calls == 2


# ---------------------------------------------------------------- 5. deadline
def test_expired_deadline_skips_retry():
    flaky = _Flaky(httpx.ConnectError("boom"), "ok")
    with pytest.raises(httpx.ConnectError):
        asyncio.run(resilience.with_retry(
            flaky.acall, attempts=3, base_delay=0.01,
            retry_on=resilience.RETRY_ON_NETWORK,
            deadline=time.monotonic() - 1.0, label="test",
        ))
    assert flaky.calls == 1


def test_deadline_too_short_for_backoff_skips_retry():
    """剩余预算不够一次退避时也不该重试 —— 否则睡完就超预算了"""
    flaky = _Flaky(httpx.ConnectError("boom"), "ok")
    with pytest.raises(httpx.ConnectError):
        asyncio.run(resilience.with_retry(
            flaky.acall, attempts=3, base_delay=1.0, jitter=False,
            retry_on=resilience.RETRY_ON_NETWORK,
            deadline=time.monotonic() + 0.05, label="test",
        ))
    assert flaky.calls == 1


def test_ample_deadline_allows_retry():
    flaky = _Flaky(httpx.ConnectError("boom"), "ok")
    result = asyncio.run(resilience.with_retry(
        flaky.acall, attempts=2, base_delay=0.01,
        retry_on=resilience.RETRY_ON_NETWORK,
        deadline=time.monotonic() + 5.0, label="test",
    ))
    assert result == "ok"
    assert flaky.calls == 2


# ---------------------------------------------------------------- 退避与判定
def test_backoff_delay_is_exponential_and_jittered():
    plain = [resilience.backoff_delay(0.5, i, jitter=False) for i in range(3)]
    assert plain == [0.5, 1.0, 2.0]

    # 抖动区间 [base*2^i, base*2^i + base]：没有抖动的话并发重试会形成惊群
    for i in range(3):
        for _ in range(20):
            delay = resilience.backoff_delay(0.5, i)
            assert 0.5 * 2 ** i <= delay <= 0.5 * 2 ** i + 0.5


def test_is_retryable_matrix():
    assert resilience.is_retryable(httpx.ConnectError("x"))
    assert resilience.is_retryable(httpx.ReadTimeout("x"))
    assert resilience.is_retryable(asyncio.TimeoutError())
    assert resilience.is_retryable(_status_error(429))
    assert resilience.is_retryable(_status_error(502))

    assert not resilience.is_retryable(ValueError("x"))
    assert not resilience.is_retryable(_status_error(400))
    assert not resilience.is_retryable(_status_error(401))
    assert not resilience.is_retryable(_status_error(404))


def test_raise_if_transient_only_for_transient_statuses():
    request = httpx.Request("GET", "https://example.invalid/x")

    resilience.raise_if_transient(httpx.Response(200, request=request))  # 不抛
    resilience.raise_if_transient(httpx.Response(404, request=request))  # 4xx 不抛

    with pytest.raises(httpx.HTTPStatusError):
        resilience.raise_if_transient(httpx.Response(503, request=request))


# ---------------------------------------------------------------- 同步版本
def test_sync_variant_transient_failure_then_success():
    flaky = _Flaky(httpx.ConnectError("boom"), "ok")
    result = resilience.with_retry_sync(
        flaky, attempts=2, base_delay=0.01,
        retry_on=resilience.RETRY_ON_NETWORK, label="test",
    )
    assert result == "ok"
    assert flaky.calls == 2


def test_sync_variant_does_not_retry_non_retryable():
    flaky = _Flaky(ValueError("bad"))
    with pytest.raises(ValueError):
        resilience.with_retry_sync(
            flaky, attempts=3, base_delay=0.01,
            retry_on=resilience.RETRY_ON_NETWORK, label="test",
        )
    assert flaky.calls == 1


def test_sync_variant_respects_deadline():
    flaky = _Flaky(httpx.ConnectError("boom"), "ok")
    with pytest.raises(httpx.ConnectError):
        resilience.with_retry_sync(
            flaky, attempts=3, base_delay=0.01,
            retry_on=resilience.RETRY_ON_NETWORK,
            deadline=time.monotonic() - 1.0, label="test",
        )
    assert flaky.calls == 1


# ---------------------------------------------------------------- 接入点真的接上了
# 光有 resilience 层的单测不够：如果某个调用点忘了接、或者接在了不会抛异常的分支上，
# 重试策略会「看起来实现了但从不生效」。下面逐个验证四处接入点。


class _StubHTTP:
    """最小 httpx.AsyncClient 替身：按脚本返回响应 / 抛异常，并记录调用次数"""

    def __init__(self, *outcomes):
        self.flaky = _Flaky(*outcomes)

    async def get(self, url, params=None, **kwargs):
        return self.flaky()

    async def post(self, url, headers=None, json=None, **kwargs):
        return self.flaky()


def _response(status: int, method: str = "GET") -> httpx.Response:
    return httpx.Response(status, request=httpx.Request(method, "https://example.invalid/x"))


def test_article_fetcher_get_retries_transient_failure():
    from src.modules.discovery.article_fetcher import ArticleFetcher

    fetcher = ArticleFetcher()
    stub = _StubHTTP(httpx.ConnectError("boom"), _response(200))
    fetcher.client = stub

    resp = asyncio.run(fetcher._get("https://example.invalid/a", params={"x": 1}))
    assert resp.status_code == 200
    assert stub.flaky.calls == 2


def test_article_fetcher_get_does_not_retry_non_transient():
    from src.modules.discovery.article_fetcher import ArticleFetcher

    fetcher = ArticleFetcher()
    stub = _StubHTTP(ValueError("bad"))
    fetcher.client = stub

    with pytest.raises(ValueError):
        asyncio.run(fetcher._get("https://example.invalid/a"))
    assert stub.flaky.calls == 1


def test_embedding_post_retries_transient_failure(fake_redis):
    from src.modules.agent.embedding_service import EmbeddingService

    service = EmbeddingService(api_key="sk-test-not-real", provider="openai")
    stub = _StubHTTP(httpx.ReadTimeout("slow"), _response(200, "POST"))
    service.http_client = stub

    resp = asyncio.run(service._post("https://example.invalid/e", headers={}, json={}))
    assert resp.status_code == 200
    assert stub.flaky.calls == 2


def test_readme_api_retries_transient_failure():
    from src.modules.discovery.github_fetcher import _readme_via_api

    stub = _StubHTTP(httpx.ConnectError("boom"), _response(404))
    assert asyncio.run(_readme_via_api("owner/repo", stub)) is None
    assert stub.flaky.calls == 2


def test_qdrant_vector_read_retries_transient_failure(fake_redis):
    """Qdrant 读取走同步客户端，验证同步重试也真的接上了"""
    from qdrant_client.http.exceptions import ResponseHandlingException

    from src.modules.discovery import tech_knowledge as tk

    class _StubQdrant:
        def __init__(self):
            self.flaky = _Flaky(ResponseHandlingException("conn reset"), ([], None))

        def scroll(self, **kwargs):
            return self.flaky()

    class _StubKB:
        def __init__(self):
            self.qdrant = _StubQdrant()

    kb = _StubKB()
    assert tk.TechKnowledgeBase.get_vectors_by_ids(kb, ["item-1"]) == {}
    assert kb.qdrant.flaky.calls == 2


# ==================================================== 观测：超时事件分类（Phase 4.3）
class APITimeoutError(Exception):  # noqa: N801 - 刻意模仿 openai 的类名
    """openai 的 APITimeoutError 继承链是 APIConnectionError → APIError，
    压根不是 TimeoutError —— 只 isinstance 会漏判，所以要按类名兜一层。
    """


class _NamedNotTimeout(Exception):
    pass


def test_is_timeout_error_recognizes_httpx_timeouts():
    assert resilience.is_timeout_error(httpx.ReadTimeout("slow"))
    assert resilience.is_timeout_error(httpx.ConnectTimeout("slow"))
    assert resilience.is_timeout_error(httpx.PoolTimeout("slow"))


def test_is_timeout_error_recognizes_asyncio_and_builtin():
    assert resilience.is_timeout_error(asyncio.TimeoutError())
    assert resilience.is_timeout_error(TimeoutError("slow"))


def test_is_timeout_error_recognizes_third_party_by_name():
    assert resilience.is_timeout_error(APITimeoutError("slow"))


def test_is_timeout_error_rejects_non_timeouts():
    assert not resilience.is_timeout_error(httpx.ConnectError("refused"))
    assert not resilience.is_timeout_error(ValueError("bad"))
    assert not resilience.is_timeout_error(_NamedNotTimeout("still not a timeout"))


def _capture_warnings(fn):
    from loguru import logger

    messages = []
    sink_id = logger.add(lambda m: messages.append(m), level="WARNING")
    try:
        fn()
    finally:
        logger.remove(sink_id)
    return "\n".join(messages)


def test_log_downstream_failure_emits_timeout_event():
    """超时要单独成事件：它指向「预算配得合不合理」，与「下游挂了」不是一类问题"""
    joined = _capture_warnings(lambda: resilience.log_downstream_failure(
        downstream="llm_chat",
        exc=httpx.ReadTimeout("slow"),
        elapsed_ms=50123,
        timeout_s=50.0,
        scope="chat",
    ))

    assert "event=downstream_timeout" in joined
    assert "downstream=llm_chat" in joined
    assert "elapsed_ms=50123" in joined
    assert "timeout_s=50.0" in joined
    assert "scope=chat" in joined
    # 两个事件都发：它们服务的告警规则不同
    assert "event=downstream_fallback" in joined
    assert "reason=timeout" in joined


def test_log_downstream_failure_uses_invoke_failed_for_other_errors():
    joined = _capture_warnings(lambda: resilience.log_downstream_failure(
        downstream="embedding",
        exc=httpx.ConnectError("refused"),
        elapsed_ms=12.0,
        scope="openai",
    ))

    assert "event=downstream_timeout" not in joined
    assert "event=downstream_fallback" in joined
    assert "reason=invoke_failed" in joined
    assert "downstream=embedding" in joined


def test_log_downstream_failure_returns_reason():
    from loguru import logger

    sink_id = logger.add(lambda m: None, level="WARNING")
    try:
        assert resilience.log_downstream_failure(
            downstream="llm_chat", exc=httpx.ReadTimeout("slow"), elapsed_ms=1.0,
        ) == "timeout"
        assert resilience.log_downstream_failure(
            downstream="llm_chat", exc=ValueError("bad"), elapsed_ms=1.0,
        ) == "invoke_failed"
    finally:
        logger.remove(sink_id)


def test_chat_route_emits_timeout_event_on_llm_timeout(client, test_user_id, monkeypatch):
    """端到端：LLM 真的超时了，日志里必须出现 event=downstream_timeout。

    超时是必须被观测的信号，不允许被 `except Exception: pass` 吞掉。
    """
    from loguru import logger

    from src.api.routes.learning import get_state_machine
    from src.modules.agent import state_machine as sm

    class _TimeoutLLM:
        def invoke(self, *args, **kwargs):
            raise httpx.ReadTimeout("upstream too slow")

    monkeypatch.setattr(sm, "llm", _TimeoutLLM())
    monkeypatch.setattr(sm, "LLM_AVAILABLE", True, raising=False)

    session_id = "timeout-observe-session"
    get_state_machine().active_sessions[session_id] = {
        "session_id": session_id,
        "user_id": str(test_user_id),
        "topic": "Python asyncio",
        "summary": "观测用会话",
        "core_concepts": ["asyncio"],
    }

    messages = []
    sink_id = logger.add(lambda m: messages.append(m), level="WARNING")
    try:
        resp = client.post(
            "/api/v1/learning/chat",
            json={"session_id": session_id, "message": "讲讲", "conversation_history": []},
        )
    finally:
        logger.remove(sink_id)

    assert resp.status_code == 200
    joined = "\n".join(messages)
    assert "event=downstream_timeout" in joined
    assert "downstream=llm_chat" in joined
    assert "scope=chat" in joined
    assert "reason=timeout" in joined
