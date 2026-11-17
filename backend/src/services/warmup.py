# Services - 进程预热
"""把「首次请求才付」的初始化成本挪到不挡路的地方。

这个项目有两笔特别贵的冷启动，量级差异很大，必须分开处理：

1. **首页个性化依赖**（`embedding_service` 导入 + 候选池一次带向量 scroll）
   —— 约 1s。首页立刻就要用，启动时就该做完，否则第一个用户的首页要等它。
2. **LLM 栈**（`langchain_openai` → `transformers` → `torch`）
   —— 本机实测 **12s**。它只服务「点开卡片 → 讲解 / 对话」这条链路，
   首页完全用不到。若在启动时就同步导入，会和用户的首个页面请求抢 GIL，
   实测把一次 50ms 的 `/discover/news` 拖到 6.7s。
   所以这里改成「首页把数据交给用户之后」再在后台导入。

结论：**该预热的立刻预热，会挡路的延后预热**。两者都是 best-effort，
失败只意味着「首次点击慢一些」，绝不影响功能可用。
"""

import asyncio
import os
import sys
import threading

from loguru import logger


def _in_test_session() -> bool:
    """是否跑在 pytest 里

    测试不该被预热拖慢，也不该因为预热去碰真实的 Qdrant / Redis：
    `PYTEST_CURRENT_TEST` 由 pytest 在每个用例前设置，`sys.modules` 里的 pytest
    覆盖「收集期/夹具内」这两处更早的调用点。
    """
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


# 重栈只预热一次：多个请求同时触发时不能各起一个线程重复导入
_heavy_started = False
_heavy_lock = threading.Lock()


def warm_feed_deps() -> None:
    """预热首页个性化真正需要的依赖（同步，调用方负责丢线程池）

    只**导入** embedding 模块而不构造服务实例：贵的是导入（httpx 等一串），
    构造本身很便宜，且实例化时要拿 api_key、绑事件循环，留给真正用它的地方做更安全。
    """
    if _in_test_session():
        return

    from src.modules.agent import embedding_service  # noqa: F401 - 导入即目的
    from src.modules.discovery.tech_knowledge import get_knowledge_base

    # 候选池顺手建好：一次带向量 scroll（500×1536）本机约 450ms，
    # 不预热的话首个登录用户的首页要连这笔账一起付。
    get_knowledge_base().candidate_pool(item_type="repo")


def _import_llm_stack() -> None:
    """导入 LLM 栈（langchain → transformers → torch），同步、很慢"""
    from src.modules.agent import state_machine  # noqa: F401 - 导入即目的

    # 导入期只创建了客户端对象；这里再确认 embedding 的 key 存在与否不会抛错，
    # 只是让「没有 key」的情况在日志里早一点可见，而不是等用户点击。
    if not (
        os.getenv("ALIYUN_API_KEY")
        or os.getenv("VOYAGE_API_KEY")
        or os.getenv("ZHIPU_API_KEY")
    ):
        logger.info("[INFO] 未配置 embedding key，个性化将回退随机抽样")


async def run_feed_warmup() -> None:
    """预热首页依赖（异步包装，失败只记日志）

    启动期会被 await（同步热身，宁可启动晚 1s 也不让首个用户等），所以这里必须
    把异常就地消化：预热失败只意味着「首个请求慢一点」，绝不能让服务起不来。
    """
    try:
        await asyncio.to_thread(warm_feed_deps)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 预热失败只降级
        logger.warning(f"[WARN] 首页依赖预热失败（首个请求会慢一些）: {exc}")


async def _run_heavy() -> None:
    try:
        await asyncio.to_thread(_import_llm_stack)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 预热失败只降级，不影响可用性
        logger.warning(f"[WARN] LLM 栈预热失败（首次点击会慢一些）: {exc}")


def schedule_heavy_warmup(delay: float = 1.5) -> None:
    """预约一次 LLM 栈预热（幂等；必须在事件循环内调用）

    delay 不是随手取的：调用点在首页请求里，重栈导入会占住 GIL 十几秒，
    如果立刻开始，用户这次 `/discover/news` 的响应就会被拖慢。留 1.5s
    足够把响应发出去，又能在用户读首屏的这段时间把重活干完。
    """
    global _heavy_started
    if _in_test_session():
        return
    with _heavy_lock:
        if _heavy_started:
            return
        _heavy_started = True

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 没有事件循环（脚本 / 离线任务）：不值得为它起线程
        return
    loop.call_later(delay, lambda: asyncio.create_task(_run_heavy()))


__all__ = ["run_feed_warmup", "schedule_heavy_warmup", "warm_feed_deps"]
