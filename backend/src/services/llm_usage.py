# Services - LLM 用量记账
"""每一次 LLM 调用都留一行账：多少 token、多少钱、多少钱一秒、成功与否。

## 为什么做成「包住 invoke 的一层」而不是在每个路由里手写埋点
埋点最怕漏。当前有 4 条真实调用链（讲解 / 对话 / 卡片导读 / 批量摘要），
将来还会加（测验生成、复盘总结）。如果每个调用点自己 `record(...)`，
新加一条链路时「忘了记账」不会有任何报错 —— 成本面板会静默地少一块。
把 `invoke` 包进 `tracked_invoke` 后，调用点只剩一行，且**异常路径也自动记账**。

## 三条纪律
1. **记账绝不阻断业务**。写库失败只打 warning，返回值照常交给调用方；
   「账单少一行」远比「用户提问失败」可接受。
2. **失败也记，但不算钱**。失败行 `success=False`、金额 0：它是失败率的分母，
   不是成本。悄悄丢掉失败会导致「重试风暴」在成本曲线上完全不可见。
3. **拿不到 usage 就估算并标记**。宁可给一个标明「估算」的数字，
   也不要给 0 —— 0 会被读成「这次免费」。
"""

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from loguru import logger

from src.core.context import get_request_id
from src.core.db import get_session_factory, is_database_configured
from src.core.pricing import (
    compute_cost_cny,
    estimate_tokens,
    extract_token_usage,
    flatten_payload,
    model_of,
)
from src.models.llm_usage import LlmUsage

# 用途 → 中文名。UI 的成本拆分直接用这份标签，避免前端再维护一遍映射。
PURPOSE_LABELS: Dict[str, str] = {
    "explain": "讲解生成",
    "chat": "学习对话",
    "digest": "卡片导读",
    "summary": "批量摘要",
    "embedding": "向量化",
    "quiz": "测验生成",
    "other": "其他",
}


@dataclass
class LlmCall:
    """一次调用的记账结果（也用于测试断言，不落库时也能检查）"""

    purpose: str
    model: str
    provider: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_cny: float
    latency_ms: int
    success: bool
    tokens_estimated: bool
    price_known: bool
    error_code: Optional[str] = None


def _provider_of(model: str) -> str:
    """从模型名反推 Provider（比读 base_url 稳：自定义网关常把 url 藏起来）"""
    name = (model or "").lower()
    if name.startswith("deepseek"):
        return "deepseek"
    if name.startswith("qwen"):
        return "dashscope"
    if name.startswith("gpt") or name.startswith("text-embedding"):
        return "openai"
    if name.startswith("claude"):
        return "anthropic"
    return "custom"


def build_call(
    *,
    purpose: str,
    model: str,
    payload: Any,
    result: Any = None,
    latency_ms: int = 0,
    success: bool = True,
    error_code: Optional[str] = None,
) -> LlmCall:
    """把「入参 + 上游返回」折算成一条账（纯函数，不碰数据库）

    token 优先取上游 usage；拿不到时用字符估算，并置 `tokens_estimated=True`。
    成功但 usage 缺失的情况真实存在（个别兼容网关），所以两条路都要有。
    """
    usage = extract_token_usage(result) if success and result is not None else None
    if usage is not None:
        prompt_tokens, completion_tokens, total_tokens = usage
        estimated = False
    else:
        prompt_tokens = estimate_tokens(flatten_payload(payload))
        # 失败时输出未知：按 0 记，避免凭空放大金额
        completion_tokens = (
            estimate_tokens(getattr(result, "content", "") or "") if success else 0
        )
        total_tokens = prompt_tokens + completion_tokens
        estimated = True

    cost_cny, price_known = compute_cost_cny(
        model,
        prompt_tokens if success else 0,
        completion_tokens if success else 0,
    )
    return LlmCall(
        purpose=purpose,
        model=model or "unknown",
        provider=_provider_of(model),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_cny=cost_cny,
        latency_ms=max(0, int(latency_ms)),
        success=success,
        tokens_estimated=estimated,
        price_known=price_known,
        error_code=error_code,
    )


def _write_row(call: LlmCall, user_id: Optional[str], request_id: Optional[str]) -> None:
    """落一行账（best-effort：任何异常都只告警）"""
    if not is_database_configured():
        return

    owner = None
    if user_id:
        try:
            owner = uuid.UUID(str(user_id))
        except (TypeError, ValueError):
            # 传了非 UUID 的调用方（历史遗留的 "demo-user"）不该让记账崩掉
            owner = None

    try:
        with get_session_factory()() as session:
            session.add(
                LlmUsage(
                    purpose=call.purpose,
                    model=call.model,
                    provider=call.provider,
                    user_id=owner,
                    prompt_tokens=call.prompt_tokens,
                    completion_tokens=call.completion_tokens,
                    total_tokens=call.total_tokens,
                    cost_cny=call.cost_cny,
                    latency_ms=call.latency_ms,
                    success=call.success,
                    tokens_estimated=call.tokens_estimated,
                    request_id=request_id,
                    error_code=call.error_code,
                )
            )
            session.commit()
    except Exception as e:  # noqa: BLE001 - 记账失败不影响业务
        logger.warning(f"[WARN] LLM 用量落库失败（已忽略）: {e}")


async def record_call(
    call: LlmCall, *, user_id: Optional[str] = None
) -> Optional[LlmCall]:
    """异步落库；返回同一个 call 便于链式使用"""
    request_id = get_request_id()
    try:
        # 卸载到线程池：SQLAlchemy 同步驱动会阻塞事件循环，
        # 而这条路径就在用户等待的那次请求里，不能让它拖慢响应。
        await asyncio.to_thread(_write_row, call, user_id, request_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] LLM 用量记账异常（已忽略）: {e}")
    return call


async def tracked_invoke(
    llm: Any,
    payload: Any,
    *,
    purpose: str,
    user_id: Optional[str] = None,
    timeout: Optional[float] = None,
):
    """调用 `llm.invoke` 并记账。

    - 与原调用**完全同构**：同步 invoke 卸载到线程池、异常原样抛出，
      因此调用方的 try/except、熔断、降级逻辑都不需要改。
    - 记账只在调用返回后发生，且永不抛错。
    """
    if llm is None:
        raise ValueError("LLM 客户端不可用，应先走降级分支")

    model = model_of(llm)
    started = time.monotonic()
    try:
        result = await asyncio.to_thread(llm.invoke, payload, timeout=timeout)
    except Exception as exc:
        await record_call(
            build_call(
                purpose=purpose,
                model=model,
                payload=payload,
                latency_ms=(time.monotonic() - started) * 1000,
                success=False,
                error_code=type(exc).__name__,
            ),
            user_id=user_id,
        )
        raise
    await record_call(
        build_call(
            purpose=purpose,
            model=model,
            payload=payload,
            result=result,
            latency_ms=(time.monotonic() - started) * 1000,
        ),
        user_id=user_id,
    )
    return result
