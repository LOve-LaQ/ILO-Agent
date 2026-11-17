# Tests - LLM 成本账本与面板接口
"""阶段 7 验收：明细落库 → 汇总 → 预测。

两条链路分开验证：
1. `record_call` 真的把一行写进 `llm_usage`（记账链路的终点）；
2. `GET /admin/llm/usage` 把明细折成总量/拆分/预测（面板的唯一数据源）。

【为什么用「先取基线再比增量」】这套用例跑在**真实开发库**上。断言绝对值会被
别的测试或手工调用留下的历史行影响；比增量则只关心「我刚写的那几行有没有被算进去」。
"""

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, func, select

from src.core.db import get_session_factory, is_database_configured
from src.models.llm_usage import LlmUsage
from src.models.user import User
from src.services.llm_cost import CN_TZ
from src.services.llm_usage import build_call, record_call

pytestmark = pytest.mark.skipif(
    not is_database_configured(),
    reason="需要 DATABASE_URL（本机 PostgreSQL + backend/.env）",
)

EMAIL_PREFIX = "costtest_"
REQUEST_PREFIX = "pytest-cost-"
PASSWORD = "Str0ng-Passw0rd"


def _hdr(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _register(client) -> dict:
    token = uuid.uuid4().hex[:10]
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{EMAIL_PREFIX}{token}@example.com",
            "username": f"cost_{token}",
            "password": PASSWORD,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _promote(email: str) -> None:
    with get_session_factory()() as session:
        user = session.execute(select(User).where(User.email == email)).scalars().one()
        user.role = "admin"
        session.commit()


def _insert(
    *,
    purpose: str,
    cost: float,
    tokens: int = 1000,
    latency_ms: int = 800,
    success: bool = True,
    days_ago: int = 0,
    model: str = "deepseek-chat",
    provider: str = "deepseek",
) -> None:
    with get_session_factory()() as session:
        session.add(
            LlmUsage(
                purpose=purpose,
                model=model,
                provider=provider,
                prompt_tokens=tokens // 2,
                completion_tokens=tokens - tokens // 2,
                total_tokens=tokens,
                cost_cny=cost,
                latency_ms=latency_ms,
                success=success,
                tokens_estimated=False,
                created_at=datetime.now(CN_TZ) - timedelta(days=days_ago),
                request_id=f"{REQUEST_PREFIX}{uuid.uuid4().hex[:8]}",
            )
        )
        session.commit()


def _purge() -> None:
    with get_session_factory()() as session:
        session.execute(
            delete(LlmUsage).where(LlmUsage.request_id.like(f"{REQUEST_PREFIX}%"))
        )
        session.execute(delete(User).where(User.email.like(f"{EMAIL_PREFIX}%")))
        session.commit()


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    _purge()
    yield
    _purge()


@pytest.fixture(autouse=True)
def _isolated_cookie_jar(anonymous_client):
    anonymous_client.cookies.clear()
    yield
    anonymous_client.cookies.clear()


def _admin_headers(client) -> dict:
    admin = _register(client)
    _promote(admin["user"]["email"])
    return _hdr(admin["access_token"])


# ==================== 权限 ====================


def test_usage_requires_login(anonymous_client):
    resp = anonymous_client.get("/api/v1/admin/llm/usage")
    assert resp.status_code == 401


def test_usage_is_admin_only(anonymous_client):
    user = _register(anonymous_client)
    resp = anonymous_client.get(
        "/api/v1/admin/llm/usage", headers=_hdr(user["access_token"])
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "ADMIN_REQUIRED"


# ==================== 记账落库 ====================


def test_record_call_writes_a_row():
    with get_session_factory()() as session:
        before = session.scalar(
            select(func.count(LlmUsage.id)).where(LlmUsage.purpose == "digest")
        )

    call = build_call(
        purpose="digest",
        model="deepseek-chat",
        payload="一段用于估算的提示词",
        result=None,
        latency_ms=1234,
        success=True,
    )
    # 覆盖 request_id 前缀，方便清理（record_call 会自己取当前请求 id，这里是 None）
    asyncio.run(record_call(call))

    with get_session_factory()() as session:
        after = session.scalar(
            select(func.count(LlmUsage.id)).where(LlmUsage.purpose == "digest")
        )
        row = session.execute(
            select(LlmUsage)
            .where(LlmUsage.purpose == "digest")
            .order_by(LlmUsage.created_at.desc())
            .limit(1)
        ).scalars().one()

    assert after == before + 1
    assert int(row.latency_ms) == 1234
    assert float(row.cost_cny) >= 0
    # 没给 result（没有上游 usage），token 必须被标记为估算
    assert row.tokens_estimated is True

    # 清理这一行（它没有 request_id 前缀）
    with get_session_factory()() as session:
        session.execute(delete(LlmUsage).where(LlmUsage.id == row.id))
        session.commit()


# ==================== 汇总与预测 ====================


def test_report_aggregates_inserted_rows(anonymous_client):
    headers = _admin_headers(anonymous_client)
    baseline = anonymous_client.get(
        "/api/v1/admin/llm/usage?days=7", headers=headers
    )
    assert baseline.status_code == 200, baseline.text
    base = baseline.json()
    base_calls = base["totals"]["calls"]
    base_cost = base["totals"]["cost_cny"]
    # 按用途/模型也要用增量比较：库里可能已有真实调用（开发库是共享的），
    # 断言绝对值会在「跑过演示数据」之后莫名其妙地失败
    base_purpose = {item["purpose"]: item["cost_cny"] for item in base["by_purpose"]}
    base_model = {item["model"]: item["cost_cny"] for item in base["by_model"]}

    _insert(purpose="chat", cost=0.02, tokens=2000, days_ago=0)
    _insert(purpose="chat", cost=0.01, tokens=1000, days_ago=1)
    _insert(
        purpose="summary",
        cost=0.05,
        tokens=8000,
        days_ago=0,
        model="qwen-plus",
        provider="dashscope",
    )
    # 失败行：计入调用数与失败率，但不计入金额
    _insert(purpose="chat", cost=0.0, tokens=500, success=False, days_ago=0)

    resp = anonymous_client.get("/api/v1/admin/llm/usage?days=7", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["window_days"] == 7
    assert len(body["series"]) == 7
    assert body["totals"]["calls"] == base_calls + 4
    assert body["totals"]["cost_cny"] == pytest.approx(base_cost + 0.08, abs=1e-4)
    # 失败率：4 次里 1 次失败，窗口内至少 0.25
    assert body["totals"]["success_rate"] < 1.0

    purposes = {item["purpose"]: item for item in body["by_purpose"]}
    assert purposes["chat"]["cost_cny"] == pytest.approx(
        base_purpose.get("chat", 0.0) + 0.03, abs=1e-4
    )
    assert purposes["summary"]["cost_cny"] == pytest.approx(
        base_purpose.get("summary", 0.0) + 0.05, abs=1e-4
    )
    assert purposes["chat"]["label"] == "学习对话"

    models = {item["model"]: item for item in body["by_model"]}
    assert models["qwen-plus"]["provider"] == "dashscope"
    assert models["qwen-plus"]["price_known"] is True
    assert models["qwen-plus"]["cost_cny"] == pytest.approx(
        base_model.get("qwen-plus", 0.0) + 0.05, abs=1e-4
    )

    # 今天的桶里应该包含刚写进去的两条成功调用
    assert body["series"][-1]["calls"] >= 3
    assert body["series"][-1]["cost_cny"] >= 0.07


def test_report_includes_forecast_and_budget(anonymous_client):
    headers = _admin_headers(anonymous_client)
    # 制造一条上升趋势，让回归有东西可拟合
    for offset, cost in enumerate((0.01, 0.02, 0.03, 0.04, 0.05)):
        _insert(purpose="chat", cost=cost, days_ago=offset)

    resp = anonymous_client.get(
        "/api/v1/admin/llm/usage?days=14", headers=headers
    )
    assert resp.status_code == 200, resp.text
    forecast = resp.json()["forecast"]

    assert forecast["method"] == "least_squares"
    assert forecast["samples"] == 14
    assert forecast["next_7d_cny"] >= 0
    assert 0 <= forecast["r2"] <= 1
    assert forecast["confidence"] in {"high", "medium", "low", "insufficient"}
    assert forecast["month_remaining_days"] >= 0
    # 本月预测 ≥ 本月已花（除非未来预测为 0）
    assert forecast["month_projection_cny"] >= forecast["mtd_cny"]
    if forecast["budget_cny"] > 0:
        assert forecast["budget_used_ratio"] is not None


def test_report_notes_are_human_readable(anonymous_client):
    headers = _admin_headers(anonymous_client)
    _insert(purpose="explain", cost=0.03, tokens=4000)

    body = anonymous_client.get(
        "/api/v1/admin/llm/usage?days=1", headers=headers
    ).json()

    assert body["currency"] == "CNY"
    assert body["timezone"] == "Asia/Shanghai"
    assert body["notes"], "面板必须给出至少一句结论，而不是只丢一堆数字"
    assert any("次调用" in note for note in body["notes"])
    assert "单价" in body["price_table_note"]
