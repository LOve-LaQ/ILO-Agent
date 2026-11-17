# Services - 后台任务执行器
"""长任务异步化：任务表 + 幂等锁 + 进度快照。

【为什么需要】
「抓取最新」一次要跑 抓取 → 去重 → 数十次 LLM 摘要 → 入库，实测 1~3 分钟。
此前整条链路在**一次 HTTP 请求内**同步执行，带来三个连锁问题：

1. 浏览器与反向代理 60s 就断开，用户看到「网络错误」，而服务端其实还在继续
   抓取和调用 LLM —— 用户重试一次就再付一遍钱；
2. 前端拿不到任何真实进度，只能自己在客户端轮播 4 句文案（5.6 秒走完），
   之后一直停在最后一句，看起来像卡死；
3. 「跑了多久、卡在哪一步」只能翻服务端日志，接口层完全不可观测。

改成：接口立即返回 `task_id`（复用采集批次号），任务在事件循环里继续跑，
前端轮询 `GET /discover/tasks/{id}` 才能看到**真实**阶段与条数。

【为什么是进程内 asyncio.Task，而不是 Celery / RQ】
本项目的部署形态是单进程 uvicorn，任务本身全是 IO 等待（HTTP 抓取 + LLM 推理），
asyncio 已经能把并发吃满；引入 Celery 要额外维护 broker、worker 进程、结果后端，
对演示项目是负资产。**边界必须讲清楚**：多 worker 或多机部署时，内存任务表会
各看各的（A worker 提交、B worker 就查不到），届时需要把进度也写进 Redis，
或整体换成外部队列。幂等锁已经放在 Redis 上，所以「多 worker 不重复触发」
这一层现在就是对的。

【幂等锁为什么必须 fail-closed】
限流是 fail-open（Redis 挂了就放行，理由见 rate_limit.py）：少一层防护好过
整个接口不可用。但幂等锁保护的是**钱**——重复触发等于重复调用 LLM。
所以 Redis 不可用时降级为**进程内 dict 锁**而不是直接放行：单进程下这依然
能挡住重复提交，既不会重复付费，也不会因为 Redis 挂了就不能抓取。
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger

from src.core.redis_client import get_redis

KEY_PREFIX = "task"

# 锁的兜底 TTL：正常路径由任务结束主动释放，这个 TTL 只兜「进程被 kill」的情况。
# 取 30 分钟，远大于单次抓取的 3 分钟，避免长时间任务跑到一半锁先过期。
LOCK_TTL_SECONDS = 1800

# 进程内保留的已完成任务上限。前端拿到终态后就不再轮询，但用户可能刷新页面
# 回来再看一眼结果；留一批足够，无上限会随运行时长无限增长。
MAX_FINISHED_TASKS = 50

# 阶段 -> 中文文案。放在后端而不是前端，是因为「进度到哪一步了」是服务端的事实，
# 两端各写一份必然漂移（前端那份曾经就是纯假文案）。
STAGE_LABELS: Dict[str, str] = {
    "queued": "任务已提交…",
    "fetching": "正在抓取上游数据…",
    "deduping": "正在与已有内容比对…",
    "summarizing": "通义千问提炼中文摘要…",
    "enriching": "正在补全原文快照…",
    "storing": "正在写入知识库…",
    "done": "已完成",
    "failed": "已失败",
}

# 终态：前端轮询到这里就可以停
TERMINAL_STATUSES = frozenset({"succeeded", "partial", "failed", "empty"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TaskState:
    """一个后台任务的实时状态（内存对象，前端每次轮询读它）

    与 `collection_batches` 的分工：批次表是**终态真相**（进程重启后仍在），
    这里是**中间进度**（重启即丢）。两者用同一个 id 关联，所以重启后前端
    仍能通过批次表拿到结局，只是看不到过程。
    """

    task_id: str
    kind: str
    status: str = "running"
    stage: str = "queued"
    processed: int = 0
    total: int = 0
    new_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    message: Optional[str] = None
    error: Optional[str] = None
    started_at: datetime = field(default_factory=_now)
    finished_at: Optional[datetime] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def advance(self, stage: str, processed: Optional[int] = None, total: Optional[int] = None) -> None:
        """推进阶段与计数

        **阶段一变就清零**：`total` 在每个阶段含义不同（抓到的条数 / 待摘要的
        条数 / 待入库的条数），跨阶段沿用会让进度条出现「23/50 就已经 100%」
        这种明显不可信的假象 —— 而进度条一旦说谎，它比没有更糟。
        """
        if stage != self.stage:
            self.stage = stage
            self.processed = 0
            self.total = 0
        if total is not None:
            self.total = max(0, int(total))
        if processed is not None:
            self.processed = max(0, int(processed))

    def snapshot(self) -> Dict[str, Any]:
        """转成可直接给前端的纯量字典"""
        elapsed_ms = int(
            ((self.finished_at or _now()) - self.started_at).total_seconds() * 1000
        )
        percent = 0
        if self.total > 0:
            percent = min(100, int(self.processed / self.total * 100))
        elif self.is_terminal:
            percent = 100

        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "status": self.status,
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "processed": self.processed,
            "total": self.total,
            "percent": percent,
            "new_count": self.new_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "message": self.message,
            "error": self.error,
            "elapsed_ms": max(0, elapsed_ms),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


# ==================== 任务表（进程内） ====================

_TASKS: Dict[str, TaskState] = {}
# 保留 asyncio.Task 的强引用：事件循环只持有弱引用，不存会被 GC 提前回收，
# 任务就静默消失（官方文档明确警告过这一点）。
_BACKGROUND: set[asyncio.Task] = set()


def get_task(task_id: str) -> Optional[TaskState]:
    return _TASKS.get(task_id)


def list_tasks(limit: int = 20) -> List[TaskState]:
    """按开始时间倒序列出内存任务（运维/调试用）"""
    return sorted(_TASKS.values(), key=lambda t: t.started_at, reverse=True)[:limit]


def _remember(state: TaskState) -> None:
    _TASKS[state.task_id] = state
    finished = [t for t in _TASKS.values() if t.is_terminal]
    if len(finished) > MAX_FINISHED_TASKS:
        finished.sort(key=lambda t: t.finished_at or t.started_at)
        for stale in finished[: len(finished) - MAX_FINISHED_TASKS]:
            _TASKS.pop(stale.task_id, None)


def reset_tasks() -> None:
    """清空任务表（测试用）"""
    _TASKS.clear()
    _BACKGROUND.clear()
    _LOCAL_LOCKS.clear()


# ==================== 幂等锁 ====================

_LOCAL_LOCKS: Dict[str, str] = {}


def _lock_key(scope: str, subject: str) -> str:
    return f"{KEY_PREFIX}:lock:{scope}:{subject}"


def acquire(scope: str, subject: str, *, task_id: str) -> Optional[str]:
    """尝试占锁。

    成功返回 None；已被占用时返回**持有者的 task_id**（前端据此接着看那个任务，
    而不是傻等或报错）。

    两层：Redis 负责跨进程，进程内 dict 负责「Redis 不可用」和「同进程快速失败」。
    即使 Redis 抢锁成功也写一份本地锁 —— 否则 Redis 中途挂掉，同进程的第二次
    提交就没人挡了。
    """
    key = _lock_key(scope, subject)

    client = get_redis()
    if client is not None:
        try:
            if not client.set(key, task_id, nx=True, ex=LOCK_TTL_SECONDS):
                holder = client.get(key)
                return holder or "unknown"
        except Exception as e:  # noqa: BLE001 - 降级到本地锁，绝不因此放行
            logger.warning(f"[WARN] 幂等锁（Redis）失败，退化为进程内锁: {e}")

    existing = _LOCAL_LOCKS.get(key)
    if existing is not None:
        return existing
    _LOCAL_LOCKS[key] = task_id
    return None


def release(scope: str, subject: str, *, task_id: str) -> None:
    """释放自己持有的锁

    先比对持有者再删：避免把「锁过期后别人抢到的新锁」删掉。
    正常路径下 TTL（30min）远大于任务时长（1~3min），这个窗口实际不会出现；
    真正需要担心的是进程被 kill —— 那种情况本来就只能靠 TTL 自然过期。
    """
    key = _lock_key(scope, subject)

    client = get_redis()
    if client is not None:
        try:
            if client.get(key) == task_id:
                client.delete(key)
        except Exception as e:  # noqa: BLE001 - 释放失败最多让锁等到 TTL 过期
            logger.warning(f"[WARN] 释放幂等锁（Redis）失败: {e}")

    if _LOCAL_LOCKS.get(key) == task_id:
        _LOCAL_LOCKS.pop(key, None)


# ==================== 提交与执行 ====================

Runner = Callable[[TaskState], Awaitable[None]]


async def _execute(
    state: TaskState,
    runner: Runner,
    *,
    scope: str,
    subject: str,
) -> None:
    """在后台跑任务，无论成败都把终态落定（绝不留下永远 running 的任务）"""
    try:
        await runner(state)
        if not state.is_terminal:
            # runner 没显式定状态时按成功算：能正常跑完就是成功，
            # 具体是 partial 还是 failed 由 runner 自己判断（它才看得到计数）。
            state.status = "succeeded"
    except asyncio.CancelledError:
        state.status = "failed"
        state.error = "任务被取消（服务正在关闭）"
        state.stage = "failed"
        state.finished_at = _now()
        raise
    except Exception as e:  # noqa: BLE001 - 任务异常必须落进状态，否则前端永远转圈
        logger.opt(exception=e).error(f"❌ 后台任务失败 {state.task_id}: {e}")
        state.status = "failed"
        state.error = str(e)[:500]
        state.stage = "failed"
        state.finished_at = _now()
    else:
        if state.stage not in ("failed",):
            state.stage = "done"
        state.finished_at = _now()
    finally:
        # to_thread：Redis 往返是同步 IO，任务回收路径同样不能阻塞事件循环
        # （这里还可能有别的任务在用这个循环）。
        await asyncio.to_thread(release, scope, subject, task_id=state.task_id)


def spawn(
    state: TaskState,
    runner: Runner,
    *,
    scope: str,
    subject: str,
) -> asyncio.Task:
    """起一个后台任务。

    **调用方必须先 `acquire()` 成功**，且 `state.task_id` 必须与占锁时用的 id
    一致 —— 任务结束时要靠它释放锁。故意不在这里自动抢锁：路由需要先拿到
    task_id 去开采集批次（两者是同一个 id），抢锁与建任务的先后顺序必须由
    调用方掌握。
    """
    _remember(state)
    task = asyncio.create_task(_execute(state, runner, scope=scope, subject=subject))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


async def shutdown(timeout: float = 5.0) -> None:
    """应用关闭时取消未完成任务（否则进程退出会留下 running 的孤儿批次）

    只等一小会儿：抓取任务本身不可中断，硬等会让热重载卡住十几秒。
    等不到就取消，孤儿批次由 `recover_orphan_batches` 在下次启动时收尾。
    """
    pending = [t for t in _BACKGROUND if not t.done()]
    if not pending:
        return

    logger.info(f"服务关闭：取消 {len(pending)} 个未完成后台任务")
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


__all__ = [
    "LOCK_TTL_SECONDS",
    "STAGE_LABELS",
    "TERMINAL_STATUSES",
    "TaskState",
    "acquire",
    "get_task",
    "list_tasks",
    "release",
    "reset_tasks",
    "shutdown",
    "spawn",
]
