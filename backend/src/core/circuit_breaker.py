# Circuit Breaker - 下游熔断（Redis 计数实现）
"""下游持续故障时，不再让每个请求都去撞一次。

【状态机】
    CLOSED    连续失败 >= N（默认 5）→ OPEN
    OPEN      直接降级、不再发起调用；M 秒（默认 30）后 → HALF_OPEN
    HALF_OPEN 只放行 1 个探测请求：成功 → CLOSED；失败 → OPEN

【为什么状态必须放 Redis 而不是进程内变量】
多实例部署时，进程内变量会让每个实例各自熔断、各自半开 —— 等于没有熔断：
下游已经挂了，实例 A 熔断了，实例 B 仍在每个请求上撞一次、各等一个超时。

【Redis 不可用时 fail-closed 到「不熔断」】
熔断是**保护措施**，不该因为保护机制本身故障而阻断主流程。Redis 挂了就视为
CLOSED，让请求照常走 —— 此时「少一层保护」远比「全站不可用」轻。

【适用范围】LLM / Embedding / Qdrant 这类外部依赖，故障可以持续一段时间。
**PostgreSQL 明确不适用**：真相源不可用是严重故障，熔断会把它掩盖成「偶发降级」，
应该直接报错。验证码校验同理（安全场景 fail-closed，不靠熔断）。

【半开为什么必须抢令牌】开放期结束的瞬间如果直接放行，全部并发请求会一起冲下去，
把刚恢复的下游再次打挂 —— 那就不是熔断而是「定时惊群」。用 SET NX 只放一个进去。
"""

import time
from typing import Tuple

from loguru import logger

from src.core.config import settings
from src.core.redis_client import get_redis

# 键前缀：与业务键（session:、crawled:、rl:）分开，便于运维目视区分
KEY_PREFIX = "cb"

# 下游标识（接入点用这些常量，避免各处手写字符串导致统计口径分裂）
LLM_CHAT = "llm_chat"
LLM_SUMMARY = "llm_summary"
EMBEDDING = "embedding"
QDRANT_READ = "qdrant_read"

# open_until 键的 TTL 系数。必须**长于**开放期本身：开放期结束后键若立刻消失，
# 就无从判断「刚才处于 OPEN」，半开逻辑会退化成「全部放行」。
_OPEN_KEY_TTL_FACTOR = 2


class CircuitOpenError(RuntimeError):
    """熔断打开：本次调用被主动跳过。

    与「下游真的报错」区分开 —— 降级日志里的 reason 不一样，告警规则也不该一样。
    """


def _keys(scope: str) -> Tuple[str, str, str]:
    return (
        f"{KEY_PREFIX}:{scope}:failures",
        f"{KEY_PREFIX}:{scope}:open_until",
        f"{KEY_PREFIX}:{scope}:probe",
    )


def _threshold() -> int:
    return int(settings.circuit_failure_threshold)


def _open_seconds() -> int:
    return max(1, int(settings.circuit_open_seconds))


def is_open(scope: str) -> bool:
    """是否应直接降级（True = 不要发起调用）。Redis 不可用时一律返回 False。"""
    client = get_redis()
    if client is None:
        return False

    _, open_until_key, probe_key = _keys(scope)
    try:
        open_until = client.get(open_until_key)
        if open_until is None:
            return False  # CLOSED

        if time.time() < float(open_until):
            return True  # OPEN：开放期内一律降级

        # 开放期已过 → HALF_OPEN：抢到探测令牌的放行，其余继续降级
        acquired = client.set(probe_key, "1", ex=_open_seconds(), nx=True)
        return not acquired
    except Exception as e:  # noqa: BLE001 - 保护机制自身故障不该阻断主流程
        logger.warning(f"[WARN] 熔断状态读取失败，本次按未熔断处理: {scope} - {e}")
        return False


def record_failure(scope: str) -> None:
    """记一次失败；达到阈值即转入 OPEN"""
    client = get_redis()
    if client is None:
        return

    threshold = _threshold()
    if threshold <= 0:
        return  # 配 0 或负数表示「该下游不熔断」，便于排查时临时放行

    failures_key, open_until_key, probe_key = _keys(scope)
    open_seconds = _open_seconds()
    try:
        pipe = client.pipeline()
        pipe.incr(failures_key)
        pipe.ttl(failures_key)
        count, ttl = pipe.execute()
        count = int(count)

        # 失败计数按开放期滚动：TTL 到期即清零，避免「上周失败 3 次 + 今天 2 次」
        # 被当成连续失败。ttl < 0 兜住「键在但没有过期时间」的异常状态。
        if count == 1 or int(ttl) < 0:
            client.expire(failures_key, open_seconds)

        # 探测失败要把令牌放回去，否则半开之后再也没有人能探测
        client.delete(probe_key)

        if count >= threshold:
            client.set(
                open_until_key,
                time.time() + open_seconds,
                ex=open_seconds * _OPEN_KEY_TTL_FACTOR,
            )
            logger.warning(
                "event=circuit_open downstream={} failures={} open_seconds={}",
                scope, count, open_seconds,
            )
    except Exception as e:  # noqa: BLE001 - 计数失败不该影响业务结果
        logger.warning(f"[WARN] 熔断失败计数异常: {scope} - {e}")


def record_success(scope: str) -> None:
    """记一次成功：直接回到 CLOSED（清空计数、开放期与探测令牌）"""
    client = get_redis()
    if client is None:
        return
    try:
        client.delete(*_keys(scope))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 熔断状态重置失败: {scope} - {e}")


def reset(scope: str) -> None:
    """显式重置（运维 / 测试用）；语义与 record_success 相同"""
    record_success(scope)


__all__ = [
    "EMBEDDING",
    "KEY_PREFIX",
    "LLM_CHAT",
    "LLM_SUMMARY",
    "QDRANT_READ",
    "CircuitOpenError",
    "is_open",
    "record_failure",
    "record_success",
    "reset",
]
