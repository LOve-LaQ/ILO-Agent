# Tests - 请求级超时预算（src/core/deadline.py + 中间件注入 + LLM 夹取）
"""覆盖任务书 Phase 3 验收：

1. `clamp_timeout` 的边界：无 deadline / 已过期 / 剩余小于配置值 / 剩余大于配置值
2. 中间件按路由把预算写进 `request.state.deadline`
3. **剩余预算耗尽时 LLM 调用没有真的发起**（断言调用次数为 0），而是直接降级
4. 有预算时把「剩余时间」作为本次调用的 timeout 传下去

全部离线可复现：不触网、不依赖真实 LLM。
"""

import asyncio
import time

from src.core import deadline as dl
from src.core.config import settings


# ---------------------------------------------------------------- clamp_timeout 边界
def test_clamp_timeout_without_deadline_returns_configured():
    assert dl.clamp_timeout(50.0, None) == 50.0


def test_clamp_timeout_expired_returns_zero():
    """返回 0 而不是「一个很小的正数」：后者仍会发起一次注定超时的调用"""
    assert dl.clamp_timeout(50.0, time.monotonic() - 1.0) == 0.0
    assert dl.clamp_timeout(50.0, time.monotonic()) == 0.0


def test_clamp_timeout_uses_remaining_when_smaller():
    value = dl.clamp_timeout(50.0, time.monotonic() + 3.0)
    assert 0 < value <= 3.0


def test_clamp_timeout_uses_configured_when_remaining_larger():
    assert dl.clamp_timeout(1.0, time.monotonic() + 30.0) == 1.0


def test_remaining_and_is_expired():
    assert dl.remaining(None) == dl.UNBOUNDED
    assert not dl.is_expired(None)

    future = time.monotonic() + 5.0
    assert 0 < dl.remaining(future) <= 5.0
    assert not dl.is_expired(future)

    past = time.monotonic() - 1.0
    assert dl.remaining(past) < 0
    assert dl.is_expired(past)


def test_new_deadline_is_in_the_future():
    deadline = dl.new_deadline(10.0)
    assert 0 < dl.remaining(deadline) <= 10.0


# ---------------------------------------------------------------- 中间件注入
def test_resolve_budget_maps_paths_to_settings():
    from src.api.middleware import resolve_budget

    assert resolve_budget("/api/v1/learning/chat") == settings.deadline_learning_chat_seconds
    assert resolve_budget("/api/v1/learning/session") == settings.deadline_learning_session_seconds
    assert resolve_budget("/api/v1/learning/response") == settings.deadline_learning_response_seconds
    assert resolve_budget("/api/v1/discover/news") == settings.deadline_discover_news_seconds
    assert (
        resolve_budget("/api/v1/discover/cards/card-1/digest")
        == settings.deadline_card_digest_seconds
    )

    # 没配预算的接口不设 deadline：长任务不该靠「把超时设大」来解决
    assert resolve_budget("/api/v1/discover/refresh") is None
    assert resolve_budget("/health") is None
    assert resolve_budget("/") is None


def test_resolve_budget_treats_non_positive_as_disabled(monkeypatch):
    from src.api import middleware

    monkeypatch.setattr(settings, "deadline_discover_news_seconds", 0.0)
    assert middleware.resolve_budget("/api/v1/discover/news") is None


def test_deadline_middleware_writes_request_state():
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from src.api.middleware import DeadlineMiddleware

    app = FastAPI()
    app.add_middleware(DeadlineMiddleware)

    @app.get("/api/v1/learning/chat")
    def _with_budget(request: Request):
        return {"deadline": getattr(request.state, "deadline", None)}

    @app.get("/health")
    def _without_budget(request: Request):
        return {"deadline": getattr(request.state, "deadline", None)}

    with TestClient(app) as client:
        before = time.monotonic()
        value = client.get("/api/v1/learning/chat").json()["deadline"]
        # Request.state 落在 scope["state"] 上，路由真的读得到（不是中间件私有副本）
        assert value is not None
        assert before < value <= before + settings.deadline_learning_chat_seconds + 1

        assert client.get("/health").json()["deadline"] is None


# ---------------------------------------------------------------- LLM 夹取
class _RecordingLLM:
    """记录每次调用参数；不触网"""

    def __init__(self):
        self.calls = []

    def invoke(self, *args, **kwargs):
        self.calls.append(kwargs)
        return type("_Resp", (), {"content": "真实讲解内容"})()


def _engine(monkeypatch, llm):
    from src.modules.agent import state_machine as sm

    monkeypatch.setattr(sm, "llm", llm)
    monkeypatch.setattr(sm, "LLM_AVAILABLE", True, raising=False)
    return sm.ExplanationEngine({"max_tokens": 4096})


def test_generate_does_not_call_llm_when_budget_exhausted(monkeypatch):
    """Phase 3 最关键的一条：预算耗尽时不能真的发起调用"""
    llm = _RecordingLLM()
    engine = _engine(monkeypatch, llm)

    result = asyncio.run(
        engine.generate("Python asyncio", {"summary": "s"}, deadline=time.monotonic() - 1.0)
    )

    assert llm.calls == []          # 一次都没发起
    assert isinstance(result, str) and result.strip()  # 走的是降级文案


def test_generate_clamps_timeout_to_remaining_budget(monkeypatch):
    llm = _RecordingLLM()
    engine = _engine(monkeypatch, llm)

    asyncio.run(
        engine.generate("Python asyncio", {"summary": "s"}, deadline=time.monotonic() + 3.0)
    )

    assert len(llm.calls) == 1
    assert 0 < llm.calls[0]["timeout"] <= 3.0


def test_generate_uses_configured_timeout_without_deadline(monkeypatch):
    llm = _RecordingLLM()
    engine = _engine(monkeypatch, llm)

    asyncio.run(engine.generate("Python asyncio", {"summary": "s"}))

    assert llm.calls[0]["timeout"] == settings.llm_chat_timeout


# ---------------------------------------------------------------- HTTP 层
def test_chat_route_skips_llm_when_budget_exhausted(client, test_user_id, monkeypatch):
    """端到端：中间件注入预算 → 路由夹取 → 预算耗尽时不发起 LLM 调用且不 500"""
    from src.api.routes.learning import get_state_machine
    from src.modules.agent import state_machine as sm

    llm = _RecordingLLM()
    monkeypatch.setattr(sm, "llm", llm)
    monkeypatch.setattr(sm, "LLM_AVAILABLE", True, raising=False)
    # 直接让中间件算出「已过期」的预算。比把预算配成 0.001s 更确定 ——
    # 后者依赖「请求处理耗时一定超过 1ms」，是个会偶发翻车的隐式假设。
    monkeypatch.setattr("src.api.middleware.new_deadline", lambda budget: time.monotonic() - 1.0)

    session_id = "deadline-exhausted-session"
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
