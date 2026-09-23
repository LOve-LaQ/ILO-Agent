# Deadline - 请求级超时预算的传递
"""把「这次请求还剩多少时间」从入口逐层往下传。

【为什么必须有它】每层各拍一个固定超时的做法，在链路一长就彻底失效：
Redis 1s + PostgreSQL 3s + LLM 50s = 最坏 54s，而且这是**没算重试**的数字。
有了 deadline，重试也受同一个总预算约束 —— 重试不会把总耗时撑爆。

【为什么用 monotonic 而不是 wall clock】系统时间被 NTP 调整或人工修改时，
wall clock 会跳变，导致「剩余时间」算出负数或突然变大。monotonic 只单调递增。

【约定】`deadline` 是 `time.monotonic()` 量纲的**时间点**，不是「剩余秒数」。
所有下游一律用 `remaining()` / `clamp_timeout()` 取值，不要自己减。
"""

import time
from typing import Optional

# 无预算时的剩余秒数。取 inf 而不是一个大常数：min(configured, inf) 自然得到
# configured，调用方不需要写「有没有 deadline」的分支。
UNBOUNDED = float("inf")


def new_deadline(budget_seconds: float) -> float:
    """按预算创建 deadline（monotonic 时间点）。

    预算为 0 或负数时返回「此刻」，即立刻过期 —— 配置里用 0 表示「不设预算」的
    语义由调用方（中间件）负责，不要在这里偷偷兜底。
    """
    return time.monotonic() + max(0.0, budget_seconds)


def remaining(deadline: Optional[float]) -> float:
    """剩余秒数；无 deadline 表示不设预算，返回 inf"""
    if deadline is None:
        return UNBOUNDED
    return deadline - time.monotonic()


def is_expired(deadline: Optional[float]) -> bool:
    """预算是否已耗尽；无 deadline 永不过期"""
    return deadline is not None and remaining(deadline) <= 0


def clamp_timeout(configured: float, deadline: Optional[float]) -> float:
    """把「本层配置的超时」夹到剩余预算之内。

    返回 **0** 表示预算已耗尽：调用方**不应发起调用**，直接走降级路径。

    【为什么必须能返回 0】「返回一个很小的正数」和「返回 0」有本质区别：前者仍会
    真的发起一次注定超时的调用，白白占住连接与线程，然后照样降级 —— 只是慢了一截。
    超时的意义是尽快失败并降级，不是等到底。
    """
    if deadline is None:
        return configured
    left = remaining(deadline)
    if left <= 0:
        return 0.0
    return min(configured, left)


__all__ = ["UNBOUNDED", "clamp_timeout", "is_expired", "new_deadline", "remaining"]
