# Resilience - 下游调用的重试与退避编排层
"""统一的「重试与退避」编排：只做决策，不做 IO，便于离线单测。

【设计原则：该重试的重试，不该重试的立刻降级】
本模块**不提供**「给所有调用统一套一层 retry」的开关。每个接入点必须显式声明
`attempts` / `base_delay` / `retry_on`，把「这里为什么可以重试」写在调用点旁边。

【重试判定三问】三个都是「是」才重试：
1. 幂等吗？（重试一次不会产生副作用）
2. 是瞬时错误吗？（网络抖动、上游短暂不可用）
3. 重试的代价可接受吗？（用户在等 / 请求预算够）

【明确不重试的场景与理由 —— 后续维护者请勿「顺手补上」】
- Redis：热缓存。降级到内存 / PostgreSQL 比重试划算，重试只是把请求拖长
- PostgreSQL：真相源。不可用是严重故障，重试会把它掩盖成「偶发慢」
- 验证码校验：安全场景必须 fail-closed，重试会拉长攻击窗口
- Qdrant 写：写失败不阻塞主流程，靠下一轮采集补齐
- HTTP 4xx（除 429）：Key 错了 / 参数错了，重试一万次结果一样
- JSON 解析错误：重试只会拿到同样的坏输出
- 业务异常（ILOException）：不是下游故障，重试没有意义

【为什么不用 tenacity】
本项目刻意不引入 tenacity（`requirements.txt` 里原有的那条遗留依赖已一并移除）。
到处挂 `@retry` 会把「哪些不该重试」
这个设计意图淹没在装饰器里；重试必须是显式的、场景化的、写在调用点旁边。
"""

import asyncio
import math
import random
import time
from typing import Awaitable, Callable, Optional, Tuple, Type, TypeVar

import httpx
from loguru import logger

T = TypeVar("T")

# 可重试的 HTTP 状态码：429 是「被限流，等一会儿再来」，5xx 是「上游自己出问题了」
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})

# 网络层瞬时故障。用 TransportError 这个基类而不是逐个列 ConnectError /
# ConnectTimeout / ReadTimeout：WriteError、ReadError、PoolTimeout 同样是瞬时故障，
# 逐个子类列举只会漏。它不包含 HTTPStatusError（后者要靠状态码判定）。
RETRY_ON_NETWORK: Tuple[Type[BaseException], ...] = (httpx.TransportError, asyncio.TimeoutError)


def qdrant_timeout_seconds(seconds: float) -> int:
    """把秒级超时预算转成 qdrant-client 能接受的**整数秒**。

    【为什么必须转换 —— 这是真实踩过的坑，别把它「优化」掉】
    qdrant-client 1.19 的 REST 客户端在 `qdrant_client/http/api_client.py:96` 做的是
    `kwargs["timeout"] = int(kwargs["params"]["timeout"])`，而 `params["timeout"]`
    在进入这行之前**已经被序列化成了字符串**。于是传 `6.0` 的路径是
    `str(6.0)` → `"6.0"` → `int("6.0")` → `ValueError: invalid literal for int()
    with base 10: '6.0'`。异常发生在客户端构造请求时，**请求根本发不出去**，
    所以任何非整数超时都等价于「这次 Qdrant 调用必然失败」。

    这个坑只在**按次传 `timeout=`** 时出现。构造期
    `QdrantClient(url=..., timeout=4.0)` 走的是 httpx 自己的超时，传 float 是安全的
    —— 所以不要「顺手」把构造期也包上，那是无害且多余的。

    【为什么向上取整】`int()` 会把 2.5s 截成 2s，让我们比配置的预算更早放弃。
    宁可多等不到 1 秒，也不要擅自缩短调用方给的预算。下限取 1 而不是 0：
    在 Qdrant 的语义里 `timeout=0` 是「不设超时」，那与「预算已耗尽」正好相反。
    """
    return max(1, math.ceil(seconds))


def is_retryable(exc: BaseException) -> bool:
    """统一的「这个异常值不值得重试」判定。

    【为什么必须集中判定】4xx（除 429）是「Key 错了 / 参数错了」，重试一万次结果
    一样。把这类错误也丢进重试，只会白等退避时间、白占请求预算。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in TRANSIENT_HTTP_STATUSES
    return isinstance(exc, RETRY_ON_NETWORK)


def raise_if_transient(response: httpx.Response) -> None:
    """把瞬时 HTTP 状态（429 / 5xx）转成异常，交给重试层处理。

    【为什么需要它】有些调用点用的是 `status_code != 200 → 降级` 而不是
    `raise_for_status()`。这类分支不会自然抛异常，重试层就永远看不到 5xx，
    于是「5xx 应该重试」这条策略静默失效。需要重试的调用点显式调一下本函数。
    4xx 不会被转换，保持原样返回，由调用方按既有分支处理（不重试）。
    """
    if response.status_code in TRANSIENT_HTTP_STATUSES:
        raise httpx.HTTPStatusError(
            f"transient HTTP {response.status_code}",
            request=response.request,
            response=response,
        )


# 超时类异常的类名白名单。**为什么要按类名判定**：超时异常散落在三家库里
# （httpx.TimeoutException、openai.APITimeoutError、asyncio.TimeoutError），
# 逐家 import 会把轻量的策略层拖成重依赖；而 openai 的 APITimeoutError 继承链是
# APIConnectionError → APIError，压根不是 TimeoutError，只 isinstance 会漏判。
_TIMEOUT_EXC_NAMES = frozenset({
    "TimeoutError",
    "TimeoutException",
    "APITimeoutError",
    "ReadTimeout",
    "ConnectTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "ReadTimeoutError",
    "ConnectTimeoutError",
})


def is_timeout_error(exc: BaseException) -> bool:
    """这个异常是不是「超时」。

    【为什么必须与普通失败区分】两者要触发的告警完全不同：超时频繁说明
    **超时预算配得不合理**（该调大或该异步化），普通失败说明**下游挂了**。
    混在同一个事件里，两类问题都看不清。
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return True
    return any(cls.__name__ in _TIMEOUT_EXC_NAMES for cls in type(exc).__mro__)


def log_downstream_failure(
    *,
    downstream: str,
    exc: BaseException,
    elapsed_ms: float,
    timeout_s: float = 0.0,
    scope: str = "",
) -> str:
    """下游调用失败时的统一观测出口（返回判定出的 reason，便于调用方复用）。

    输出两个事件：
    - `event=downstream_timeout`  仅超时，带 elapsed_ms / timeout_s —— 用于
      「超时预算是否合理」这条告警
    - `event=downstream_fallback` 所有失败，带 reason —— 用于「下游是否挂了」

    【为什么超时也要发 fallback】超时是必须被观测的信号，不能被
    `except Exception: pass` 吞掉；两个事件都发，是因为它们服务的告警规则不同。
    """
    scope = scope or "-"
    if is_timeout_error(exc):
        logger.warning(
            "event=downstream_timeout downstream={} scope={} elapsed_ms={:.0f} timeout_s={}",
            downstream, scope, elapsed_ms, timeout_s,
        )
        reason = "timeout"
    else:
        reason = "invoke_failed"

    logger.warning(
        "event=downstream_fallback downstream={} scope={} reason={} elapsed_ms={:.0f} error={}",
        downstream, scope, reason, elapsed_ms, exc,
    )
    return reason


def backoff_delay(base_delay: float, attempt: int, jitter: bool = True) -> float:
    """第 attempt 次重试前的等待秒数（attempt 从 0 起）。

    指数退避 + 抖动。抖动不是装饰：没有它，一批同时失败的并发请求会在同一时刻
    一起重试，把上游从「抖动」直接打成「雪崩」。
    """
    delay = base_delay * (2 ** attempt)
    if jitter:
        delay += random.uniform(0, base_delay)
    return delay


def _should_retry(
    exc: BaseException,
    retry_on: Tuple[Type[BaseException], ...],
    should_retry: Optional[Callable[[BaseException], bool]],
) -> bool:
    if should_retry is not None:
        return should_retry(exc)
    if retry_on:
        return isinstance(exc, retry_on)
    return is_retryable(exc)


def _retry_delay(
    exc: BaseException,
    *,
    attempt: int,
    attempts: int,
    base_delay: float,
    jitter: bool,
    deadline: Optional[float],
    label: str,
) -> float:
    """判定「还有没有下一次重试」并给出等待时长。

    不能重试时直接抛出原始异常（保留原始栈），所以本函数「返回 float」只是
    正常路径的契约，异常路径不会返回。
    """
    if attempt >= attempts:
        logger.warning(
            "event=downstream_retry_exhausted downstream={} attempts={} reason={}",
            label, attempts, type(exc).__name__,
        )
        raise exc

    delay = backoff_delay(base_delay, attempt, jitter)

    if deadline is not None:
        remaining = deadline - time.monotonic()
        # 两个条件都要挡：已经没预算了，或者睡完就超预算了。
        # 后者不挡的话，重试会把总耗时撑爆 —— deadline 的存在意义就是约束重试。
        if remaining <= 0 or delay >= remaining:
            logger.warning(
                "event=downstream_retry_skipped downstream={} reason=deadline "
                "attempt={} remaining_ms={:.0f} delay_ms={:.0f}",
                label, attempt + 1, remaining * 1000, delay * 1000,
            )
            raise exc

    logger.warning(
        "event=downstream_retry downstream={} attempt={} of={} reason={} delay_ms={:.0f}",
        label, attempt + 1, attempts, type(exc).__name__, delay * 1000,
    )
    return delay


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay: float,
    jitter: bool = True,
    retry_on: Tuple[Type[BaseException], ...] = (),
    should_retry: Optional[Callable[[BaseException], bool]] = None,
    deadline: Optional[float] = None,
    label: str = "",
) -> T:
    """执行 fn，失败时按策略重试。

    Args:
        fn: 无参协程工厂（延迟调用，每次重试都会重新执行）
        attempts: **首次调用之外**允许的重试次数。attempts=2 表示最多调 3 次。
        base_delay: 首次重试的基础等待秒数（后续按 2 的幂增长，再叠加抖动）
        jitter: 是否给退避加抖动，默认开
        retry_on: 允许重试的异常类型白名单；不在其中的异常原样抛出
        should_retry: 自定义判定（用于「同一异常类型里只有部分状态码可重试」，
            例如 HTTPStatusError 需要看 status_code）。给了它就以它为准。
        deadline: monotonic 时间点。剩余预算不够一次退避时不再重试（与 deadline
            传递机制联动，避免重试把总耗时撑爆）
        label: 结构化日志里的下游标识，如 "embedding" / "github_search"

    未同时指定 retry_on 与 should_retry 时，回退到 is_retryable（只认瞬时故障）。
    """
    for attempt in range(attempts + 1):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 - 非白名单异常会在下面原样抛出
            if not _should_retry(exc, retry_on, should_retry):
                raise
            delay = _retry_delay(
                exc, attempt=attempt, attempts=attempts, base_delay=base_delay,
                jitter=jitter, deadline=deadline, label=label,
            )
            await asyncio.sleep(delay)

    raise AssertionError("with_retry 不应走到这里")  # pragma: no cover


def with_retry_sync(
    fn: Callable[[], T],
    *,
    attempts: int,
    base_delay: float,
    jitter: bool = True,
    retry_on: Tuple[Type[BaseException], ...] = (),
    should_retry: Optional[Callable[[BaseException], bool]] = None,
    deadline: Optional[float] = None,
    label: str = "",
) -> T:
    """with_retry 的同步版本。

    【为什么需要它】Qdrant 的读取走同步客户端（`build_user_profile` 也在同步上下文
    里被调用），用不了 `await`。策略与判定逻辑完全共用，只有等待方式不同。
    """
    for attempt in range(attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 非白名单异常会在下面原样抛出
            if not _should_retry(exc, retry_on, should_retry):
                raise
            delay = _retry_delay(
                exc, attempt=attempt, attempts=attempts, base_delay=base_delay,
                jitter=jitter, deadline=deadline, label=label,
            )
            time.sleep(delay)

    raise AssertionError("with_retry_sync 不应走到这里")  # pragma: no cover


__all__ = [
    "RETRY_ON_NETWORK",
    "TRANSIENT_HTTP_STATUSES",
    "backoff_delay",
    "is_retryable",
    "is_timeout_error",
    "log_downstream_failure",
    "qdrant_timeout_seconds",
    "raise_if_transient",
    "with_retry",
    "with_retry_sync",
]
