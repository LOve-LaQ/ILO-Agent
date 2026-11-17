# Tests - LLM 成本预测的纯函数部分
"""不依赖数据库、不依赖网络：只验证「钱是怎么算出来的、趋势是怎么预测的」。

为什么这些逻辑要单独测：
- 成本面板上每一个数字都会被拿去和上游账单核对，算错一次就再也没人信它；
- 预测公式最容易被改坏（有人会把斜率方向、预测起点写错），而错误只表现为
  「预测值看着有点奇怪」，不会报错。
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.core.pricing import (
    compute_cost_cny,
    estimate_tokens,
    extract_token_usage,
    lookup_price,
)
from src.services.llm_cost import CN_TZ, daily_buckets, forecast_cost, linear_fit
from src.services.llm_usage import build_call, tracked_invoke


# ==================== 计价 ====================


def test_cost_uses_per_million_pricing():
    # deepseek-chat: 输入 2 元/M、输出 8 元/M
    cost, known = compute_cost_cny("deepseek-chat", 1_000_000, 0)
    assert known is True
    assert cost == pytest.approx(2.0)

    cost, _ = compute_cost_cny("deepseek-chat", 0, 1_000_000)
    assert cost == pytest.approx(8.0)


def test_cost_is_not_rounded_to_zero():
    """单次小调用必须留下非零金额，否则日汇总永远是 0"""
    cost, _ = compute_cost_cny("deepseek-chat", 800, 400)
    assert cost > 0
    assert cost < 0.01


def test_model_name_with_prefix_and_date_still_matches():
    # 上游可能返回 vendor 前缀或带日期的快照名，都要命中同一档价
    for name in ("deepseek/deepseek-chat", "deepseek-chat-0324", "DEEPSEEK-CHAT"):
        _, known = lookup_price(name)
        assert known is True, name


def test_unknown_model_falls_back_but_flags_it():
    """未知模型按兜底价算，并且必须能被识别出来（界面上要标注）"""
    _, known = lookup_price("my-private-gateway-v1")
    assert known is False
    cost, _ = compute_cost_cny("my-private-gateway-v1", 1_000_000, 0)
    assert cost > 0


def test_token_estimation_handles_cjk():
    # 中文按 0.6 token/字，600 字约 360 token
    assert 300 < estimate_tokens("技术" * 300) < 420
    assert estimate_tokens("") == 0


def test_extract_usage_from_both_shapes():
    msg = SimpleNamespace(usage_metadata={"input_tokens": 12, "output_tokens": 8})
    assert extract_token_usage(msg) == (12, 8, 20)

    # 只有原始 response_metadata 时也要能取到（部分兼容网关只有这个）
    raw = SimpleNamespace(response_metadata={"token_usage": {"prompt_tokens": 30, "completion_tokens": 7}})
    assert extract_token_usage(raw) == (30, 7, 37)

    assert extract_token_usage(SimpleNamespace()) is None


# ==================== 拟合与预测 ====================


def test_linear_fit_recovers_exact_line():
    slope, intercept, r2 = linear_fit([1, 3, 5, 7, 9])
    assert slope == pytest.approx(2.0)
    assert intercept == pytest.approx(1.0)
    assert r2 == pytest.approx(1.0)


def test_linear_fit_on_flat_series_does_not_produce_nan():
    """新部署时整条序列为 0：不能出现 nan（会让 round 抛异常）"""
    slope, intercept, r2 = linear_fit([0, 0, 0, 0])
    assert slope == 0
    assert intercept == 0
    assert r2 == 1.0


def test_forecast_extrapolates_upward_trend():
    series = [{"cost_cny": float(i)} for i in range(1, 8)]  # 1..7 元，线性上升
    result = forecast_cost(series, fit_days=7, horizon_days=7)
    assert result["samples"] == 7
    assert result["slope_cny_per_day"] == pytest.approx(1.0)
    # 未来 7 天应为 8..14，合计 77
    assert result["next_7d_cny"] == pytest.approx(77.0, abs=0.5)
    assert result["confidence"] == "high"


def test_forecast_is_clipped_at_zero():
    """下降趋势外推必然穿零，成本不能为负"""
    series = [{"cost_cny": 10.0 - i} for i in range(10)]  # 10..1
    result = forecast_cost(series, fit_days=10, horizon_days=7)
    assert result["slope_cny_per_day"] < 0
    assert all(value >= 0 for value in result["daily_forecast"])


def test_forecast_flags_insufficient_samples():
    result = forecast_cost([{"cost_cny": 1.0}, {"cost_cny": 2.0}])
    assert result["confidence"] == "insufficient"


# ==================== 日桶 ====================


def test_daily_buckets_fill_missing_days_with_zero():
    """空缺日必须补 0：漏掉零成本日会把斜率算高"""
    now = datetime(2026, 11, 16, 12, 0, tzinfo=CN_TZ)
    rows = [
        SimpleNamespace(created_at=now, cost_cny=0.5, total_tokens=100, success=True),
        SimpleNamespace(
            created_at=now - timedelta(days=2), cost_cny=0.25, total_tokens=50, success=True
        ),
    ]
    series = daily_buckets(rows, days=5, today=now.date())
    assert len(series) == 5
    # 窗口是 11-12 ~ 11-16：11-14 有 0.25，今天 0.5，其余为 0
    assert [point["date"] for point in series] == [
        "2026-11-12",
        "2026-11-13",
        "2026-11-14",
        "2026-11-15",
        "2026-11-16",
    ]
    assert [point["cost_cny"] for point in series] == [0.0, 0.0, 0.25, 0.0, 0.5]


def test_daily_buckets_use_local_timezone():
    """UTC 23:00 的调用在东八区属于「第二天」，不能算到前一天"""
    utc_late = datetime(2026, 11, 15, 23, 30, tzinfo=timezone.utc)  # = 11-16 07:30 +08
    rows = [SimpleNamespace(created_at=utc_late, cost_cny=1.0, total_tokens=10, success=True)]
    series = daily_buckets(rows, days=2, today=datetime(2026, 11, 16, tzinfo=CN_TZ).date())
    assert series[-1]["date"] == "2026-11-16"
    assert series[-1]["cost_cny"] == 1.0


def test_failed_call_keeps_tokens_out_of_cost():
    call = build_call(
        purpose="chat",
        model="deepseek-chat",
        payload="hello",
        success=False,
        latency_ms=120,
        error_code="TimeoutError",
    )
    assert call.success is False
    assert call.cost_cny == 0
    assert call.error_code == "TimeoutError"
    # 失败也要留 token 估算，便于判断「这次是不是发了超长上下文才超时」
    assert call.prompt_tokens > 0
    assert call.tokens_estimated is True


# ==================== 记账包装器 ====================


def test_tracked_invoke_records_and_returns(monkeypatch):
    recorded = []

    async def fake_record(call, *, user_id=None):
        recorded.append((call, user_id))
        return call

    monkeypatch.setattr("src.services.llm_usage.record_call", fake_record)

    class FakeLlm:
        model_name = "deepseek-chat"

        def invoke(self, payload, timeout=None):
            return SimpleNamespace(
                content="answer",
                usage_metadata={"input_tokens": 100, "output_tokens": 50},
            )

    result = asyncio.run(
        tracked_invoke(
            FakeLlm(), "hi", purpose="chat", user_id="00000000-0000-0000-0000-0000000000a1"
        )
    )
    assert result.content == "answer"
    assert len(recorded) == 1
    call, user_id = recorded[0]
    assert (call.purpose, call.model, call.total_tokens) == ("chat", "deepseek-chat", 150)
    assert call.cost_cny > 0
    assert user_id.endswith("a1")


def test_tracked_invoke_records_failure_and_reraises(monkeypatch):
    """失败必须记账后原样抛出：调用方的熔断/降级逻辑依赖这个异常"""
    recorded = []

    async def fake_record(call, *, user_id=None):
        recorded.append(call)
        return call

    monkeypatch.setattr("src.services.llm_usage.record_call", fake_record)

    class BoomLlm:
        model_name = "deepseek-chat"

        def invoke(self, payload, timeout=None):
            raise TimeoutError("upstream timeout")

    with pytest.raises(TimeoutError):
        asyncio.run(tracked_invoke(BoomLlm(), "hi", purpose="summary"))

    assert len(recorded) == 1
    assert recorded[0].success is False
    assert recorded[0].cost_cny == 0
