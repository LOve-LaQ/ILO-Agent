# Services - LLM 成本汇总与预测
"""把 `llm_usage` 的明细行折成「花了多少、花在哪、接下来会花多少」。

## 预测为什么用最小二乘线性回归，而不是「近 7 天平均 × 天数」
7 天平均是**无记忆**的：昨天成本从 5 元掉到 0.5 元，它仍会把 7 天均值当明天，
对「正在爬升」或「刚被限流」这两种最需要预警的形态反应最慢。
线性回归至少给出**方向**（slope），并且能用一个 R² 诚实地告诉使用者
「这条线到底靠不靠谱」——R² 低的时候页面直接提示「趋势不稳，仅供参考」，
比给一个精确到分的假数字负责得多。

## 为什么同时给「日均」和「本月预测」
- 日均：解释成本结构（一次对话 / 一次摘要各多少钱）
- 本月预测：直接对应账单周期，也是「预算还能撑几天」的分母

## 口径与陷阱
- 只统计 `success=True` 的金额：失败的调用不产生 token 费用（但会计入失败率）。
- 日期按 **UTC+8** 分桶。库里是 timestamptz，若按 UTC 分桶，晚上 8 点后的调用
  会算到「昨天」，国内的日成本曲线会整体错位 8 小时。
- 窗口内的空缺日必须补 0 再拟合：漏掉零成本日会把斜率算高，
  等于预测出一个用户并没有的花钱速度。
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.core.pricing import UNKNOWN_MODEL_PRICE, lookup_price
from src.models.llm_usage import LlmUsage
from src.services.llm_usage import PURPOSE_LABELS

# 展示与分桶统一用东八区：服务部署在国内，账单周期也按自然日（北京时间）算
CN_TZ = timezone(timedelta(hours=8))

# 预测窗口：用最近 14 天拟合、预测未来 7 天。
# 14 天≈两周的使用节律（工作日/周末差异），再长会把「上个版本的行为」混进来。
FIT_DAYS = 14
FORECAST_DAYS = 7


# ==================== 纯函数（可单测，不碰数据库）====================


def linear_fit(values: Sequence[float]) -> Tuple[float, float, float]:
    """最小二乘拟合 y = intercept + slope * x（x 从 0 开始）。

    返回 (slope, intercept, r2)。

    【为什么要处理方差为 0】新部署时整条序列都是 0，分母是 0 会让 slope 变成 nan，
    再往下 `round()` 直接抛 ValueError —— 一个「还没开始用」的干净状态
    不该把管理页打成 500。此时返回 (0, 均值, 1.0)：一条水平线，拟合完美。
    """
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0.0
    mean_x = (n - 1) / 2
    mean_y = sum(values) / n

    sxx = sum((i - mean_x) ** 2 for i in range(n))
    sxy = sum((i - mean_x) * (values[i] - mean_y) for i in range(n))

    if sxx == 0:
        # 只有一个样本点：无法谈趋势，按水平线处理
        return 0.0, mean_y, 1.0

    slope = sxy / sxx
    intercept = mean_y - slope * mean_x

    ss_tot = sum((y - mean_y) ** 2 for y in values)
    if ss_tot == 0:
        return slope, intercept, 1.0
    ss_res = sum((values[i] - (intercept + slope * i)) ** 2 for i in range(n))
    r2 = max(0.0, 1 - ss_res / ss_tot)
    return slope, intercept, r2


def daily_buckets(rows: Iterable[Any], days: int, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """把明细行折成「连续 N 天」的日序列（空缺日补 0）。

    入参 rows 需要具备 created_at / cost_cny / total_tokens / success 属性
    （ORM 行或同名属性的轻量对象都行，便于测试）。
    """
    today = today or datetime.now(CN_TZ).date()
    start = today - timedelta(days=days - 1)

    # 先铺满空桶：保证 series 长度恒为 days，前端画图不需要自己补洞
    buckets: Dict[date, Dict[str, Any]] = {}
    for offset in range(days):
        day = start + timedelta(days=offset)
        buckets[day] = {"date": day.isoformat(), "calls": 0, "tokens": 0, "cost_cny": 0.0}

    for row in rows:
        created = getattr(row, "created_at", None)
        if created is None:
            continue
        if created.tzinfo is None:
            # 万一有 naive 时间（手工插入 / 旧数据），按东八区解释而不是 UTC
            created = created.replace(tzinfo=CN_TZ)
        day = created.astimezone(CN_TZ).date()
        bucket = buckets.get(day)
        if bucket is None:
            continue  # 窗口外（理论上查询已过滤，防御性跳过）
        bucket["calls"] += 1
        if getattr(row, "success", True):
            bucket["tokens"] += int(getattr(row, "total_tokens", 0) or 0)
            bucket["cost_cny"] += float(getattr(row, "cost_cny", 0) or 0)

    for bucket in buckets.values():
        bucket["cost_cny"] = round(bucket["cost_cny"], 4)
    return [buckets[start + timedelta(days=i)] for i in range(days)]


def forecast_cost(
    series: Sequence[Dict[str, Any]],
    *,
    fit_days: int = FIT_DAYS,
    horizon_days: int = FORECAST_DAYS,
) -> Dict[str, Any]:
    """基于日成本序列预测未来 `horizon_days` 天。

    取序列**末尾** `fit_days` 天做拟合：窗口太长会把「上周的异常峰值」当成趋势。
    预测值裁剪到 ≥0（成本不可能为负；负斜率外推几日后必然穿零）。
    """
    values = [float(point.get("cost_cny") or 0) for point in series][-fit_days:]
    samples = len(values)
    slope, intercept, r2 = linear_fit(values)

    daily: List[float] = []
    for step in range(samples, samples + horizon_days):
        daily.append(max(0.0, intercept + slope * step))

    total = sum(daily)
    avg = total / horizon_days if horizon_days else 0.0

    if samples < 3:
        confidence = "insufficient"
    elif r2 >= 0.6:
        confidence = "high"
    elif r2 >= 0.3:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "method": "least_squares",
        "samples": samples,
        "slope_cny_per_day": round(slope, 4),
        "r2": round(r2, 3),
        "confidence": confidence,
        "next_7d_cny": round(total, 4),
        "daily_avg_cny": round(avg, 4),
        "daily_forecast": [round(value, 4) for value in daily],
    }


# ==================== 汇总（读库）====================


def _since(days: int) -> datetime:
    """窗口起点：N 天前的**零点**（东八区），转成带时区的时间给 SQL 比较"""
    today = datetime.now(CN_TZ).date()
    start_day = today - timedelta(days=days - 1)
    return datetime.combine(start_day, datetime.min.time(), tzinfo=CN_TZ)


def _month_start() -> datetime:
    now = datetime.now(CN_TZ)
    return datetime(now.year, now.month, 1, tzinfo=CN_TZ)


def _sum_cost(db: Session, since: datetime) -> Tuple[float, int]:
    """某时间点之后的 (成功调用金额合计, 调用次数)"""
    rows = (
        db.query(
            func.coalesce(func.sum(LlmUsage.cost_cny), 0),
            func.count(LlmUsage.id),
        )
        .filter(LlmUsage.created_at >= since, LlmUsage.success.is_(True))
        .one()
    )
    return float(rows[0] or 0), int(rows[1] or 0)


def _yuan(value: float) -> str:
    """金额转文字：小于 1 分时保留 4 位，否则 ¥0.0010 会被写成「¥0.00」像没花钱"""
    return f"¥{value:.2f}" if abs(value) >= 0.01 else f"¥{value:.4f}"


def _notes(
    forecast: Dict[str, Any],
    totals: Dict[str, Any],
    budget: float,
    month_projection: float,
    mtd: float,
) -> List[str]:
    """把数字翻译成可执行的一句话（面板上直接显示，不用读者自己推）"""
    notes: List[str] = []
    avg = totals["cost_cny"] / max(1, totals["calls"])
    if totals["calls"] == 0:
        notes.append("窗口内还没有 LLM 调用记录；跑一次「刷新资讯」或发一轮对话即可生成账单。")
        return notes

    notes.append(
        f"窗口内 {totals['calls']} 次调用，平均每次 ¥{avg:.4f}，"
        f"失败 {totals['calls'] - totals['success_calls']} 次。"
    )

    if forecast["confidence"] == "low":
        notes.append("近两周日成本波动较大，趋势拟合可信度低，预测仅供参考。")
    elif forecast["slope_cny_per_day"] > 0.01:
        notes.append(
            f"日成本呈上升趋势（每天 +¥{forecast['slope_cny_per_day']:.4f}），"
            "建议检查是否有新链路在放大调用量。"
        )

    if budget > 0:
        remaining = budget - mtd
        if remaining <= 0:
            notes.append("本月已超出预算，建议临时关闭高成本开关（批量摘要 / 学习对话）。")
        elif forecast["daily_avg_cny"] > 0:
            days_left = remaining / forecast["daily_avg_cny"]
            if days_left > 365:
                # 「还能撑 74997 天」既真实又毫无意义 —— 这种量级只说明一件事：
                # 在当前用量下预算根本构不成约束，如实说出来比给一个大数字有用
                notes.append(
                    f"本月已花 {_yuan(mtd)}，按当前用量推算月末约 {_yuan(month_projection)}，"
                    f"预算 {_yuan(budget)} 不构成约束。"
                )
            else:
                notes.append(
                    f"本月已花 {_yuan(mtd)} / 预算 {_yuan(budget)}，"
                    f"按当前速度还能撑约 {days_left:.1f} 天；"
                    f"月末预计 {_yuan(month_projection)}。"
                )
    return notes


def build_usage_report(
    db: Session,
    *,
    days: int = 14,
    budget_cny: float = 0.0,
) -> Dict[str, Any]:
    """成本面板所需的全部数据（一次查库，Python 聚合）。

    【为什么在 Python 里聚合而不是全用 SQL】窗口最多 90 天、明细量级在万行以内，
    一次全取比写 4 条 `GROUP BY date_trunc` 更简单，也让「补零日」「按用途打标签」
    这类展示逻辑只有一处实现。数据量再大时再换 SQL 聚合 + 物化视图，接口形状不变。
    """
    since = _since(days)
    rows = (
        db.query(LlmUsage)
        .filter(LlmUsage.created_at >= since)
        .order_by(LlmUsage.created_at.asc())
        .all()
    )

    series = daily_buckets(rows, days)

    totals = {
        "calls": len(rows),
        "success_calls": sum(1 for row in rows if row.success),
        "prompt_tokens": sum(int(row.prompt_tokens or 0) for row in rows),
        "completion_tokens": sum(int(row.completion_tokens or 0) for row in rows),
        "total_tokens": sum(int(row.total_tokens or 0) for row in rows),
        "cost_cny": round(sum(float(row.cost_cny or 0) for row in rows if row.success), 4),
        "avg_latency_ms": 0,
        "success_rate": 0.0,
        "estimated_calls": sum(1 for row in rows if row.tokens_estimated),
    }
    ok_rows = [row for row in rows if row.success]
    if ok_rows:
        totals["avg_latency_ms"] = int(
            sum(int(row.latency_ms or 0) for row in ok_rows) / len(ok_rows)
        )
    if rows:
        totals["success_rate"] = round(totals["success_calls"] / len(rows), 3)

    # ---- 按用途拆分 ----
    purpose_map: Dict[str, Dict[str, Any]] = {}
    model_map: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        purpose = row.purpose or "other"
        entry = purpose_map.setdefault(
            purpose,
            {
                "purpose": purpose,
                "label": PURPOSE_LABELS.get(purpose, purpose),
                "calls": 0,
                "tokens": 0,
                "cost_cny": 0.0,
            },
        )
        entry["calls"] += 1
        entry["tokens"] += int(row.total_tokens or 0)
        if row.success:
            entry["cost_cny"] += float(row.cost_cny or 0)

        model = row.model or "unknown"
        m_entry = model_map.setdefault(
            model,
            {
                "model": model,
                "provider": row.provider or "unknown",
                "calls": 0,
                "cost_cny": 0.0,
                "price_known": True,
            },
        )
        m_entry["calls"] += 1
        if row.success:
            m_entry["cost_cny"] += float(row.cost_cny or 0)

    total_cost = totals["cost_cny"] or 0.0
    by_purpose = []
    for entry in sorted(purpose_map.values(), key=lambda item: item["cost_cny"], reverse=True):
        entry["cost_cny"] = round(entry["cost_cny"], 4)
        entry["avg_cost_cny"] = round(entry["cost_cny"] / max(1, entry["calls"]), 6)
        entry["share"] = round(entry["cost_cny"] / total_cost, 3) if total_cost else 0.0
        by_purpose.append(entry)

    by_model = []
    for entry in sorted(model_map.values(), key=lambda item: item["cost_cny"], reverse=True):
        entry["cost_cny"] = round(entry["cost_cny"], 4)
        # 是否命中价目表：命中不到说明金额是按兜底价估的，必须在界面上标出来，
        # 否则「这个模型很便宜」可能只是一次错误的假设
        _, known = lookup_price(entry["model"])
        entry["price_known"] = known
        by_model.append(entry)

    # ---- 预测 ----
    forecast = forecast_cost(series)

    # 本月数据单独取：窗口可能没有覆盖到月初（例如看 7 天视图）
    mtd_cost, _ = _sum_cost(db, _month_start())
    now = datetime.now(CN_TZ)
    if now.month == 12:
        next_month = datetime(now.year + 1, 1, 1, tzinfo=CN_TZ)
    else:
        next_month = datetime(now.year, now.month + 1, 1, tzinfo=CN_TZ)
    remaining_days = (next_month.date() - now.date()).days
    month_projection = round(mtd_cost + forecast["daily_avg_cny"] * remaining_days, 4)

    budget = float(budget_cny or 0.0)
    days_until_budget: Optional[float] = None
    if budget > 0 and forecast["daily_avg_cny"] > 0:
        days_until_budget = round(max(0.0, (budget - mtd_cost) / forecast["daily_avg_cny"]), 1)

    forecast.update(
        {
            "mtd_cny": round(mtd_cost, 4),
            "month_remaining_days": remaining_days,
            "month_projection_cny": month_projection,
            "budget_cny": round(budget, 2),
            "budget_used_ratio": round(mtd_cost / budget, 3) if budget > 0 else None,
            "days_until_budget": days_until_budget,
        }
    )

    return {
        "window_days": days,
        "timezone": "Asia/Shanghai",
        "generated_at": now.isoformat(),
        "currency": "CNY",
        "totals": totals,
        "series": series,
        "by_purpose": by_purpose,
        "by_model": by_model,
        "forecast": forecast,
        "notes": _notes(forecast, totals, budget, month_projection, mtd_cost),
        "price_table_note": (
            f"金额在写入时按当时单价计算（未知模型按兜底价 ¥{UNKNOWN_MODEL_PRICE.input_cny}"
            f"/¥{UNKNOWN_MODEL_PRICE.output_cny} 每百万 token）。"
        ),
    }
