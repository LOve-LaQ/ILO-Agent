# Core - LLM 价目表与 token 估算
"""把「一次 LLM 调用消耗了多少 token」换算成「花了多少钱」，并给出可解释的兜底估算。

## 为什么价目表要写在代码里，而不是每次去查上游
1. **上游返回的 usage 只有 token 数，没有钱**。要算金额就必须有一份单价表，
   而单价在同一个 Provider 内是「按模型」区分的，只能本地维护。
2. **历史成本不可被改写**。单价会调（DeepSeek 一年内就改过多次），
   如果只在查询时用「当前单价 × 历史 token」，一次调价就会把过去几个月的账
   全部重算 —— 那不是一个可对账的成本系统。所以**入库时就把金额算好并落库**，
   本文件只服务于「当时那一刻」的计算。
3. 放在 `core/` 而不是 `services/`：它没有 IO、没有状态，任何模块都可以安全引用，
   测试里也能直接断言数字。

## 单位
全部按 **人民币元 / 百万 token**（CNY per 1M tokens）标价，与国内厂商的报价口径一致。
美元计价的模型按下表注释里的汇率折算（7.2）；汇率变动时直接改那几行数字。
"""

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class ModelPrice:
    """单个模型的单价（元 / 百万 token）"""

    input_cny: float
    output_cny: float
    # 说明：为什么是这个价 —— 便于日后核对，也避免「这个数字哪来的」无人可答
    note: str = ""


# 键为**模型名前缀**（小写）。查询时取最长匹配，因此
# "deepseek-chat-v3" / "deepseek-chat" 都能命中 "deepseek-chat"。
#
# 数字来源：各厂商公开价目表（2025-2026 报价，人民币口径，按量计费）。
# 【改价只需改这里】它是唯一真相源，入库金额由它算出。
PRICE_TABLE: Dict[str, ModelPrice] = {
    # ---- DeepSeek（对话主力，OpenAI 兼容协议）----
    # 输入按「缓存未命中」计：宁可按贵的算，也不要让账面比真实账单乐观。
    "deepseek-chat": ModelPrice(2.0, 8.0, "DeepSeek V3 标准价（输入缓存未命中）"),
    "deepseek-reasoner": ModelPrice(4.0, 16.0, "DeepSeek R1 标准价"),
    # ---- 通义千问（批量摘要主力，DashScope 兼容模式）----
    "qwen-plus": ModelPrice(0.8, 2.0, "qwen-plus 标准价"),
    "qwen-turbo": ModelPrice(0.3, 0.6, "qwen-turbo 标准价"),
    "qwen-max": ModelPrice(2.4, 9.6, "qwen-max 标准价"),
    # ---- OpenAI（兜底）----
    "gpt-3.5-turbo": ModelPrice(3.6, 10.8, "0.5 / 1.5 USD，按 7.2 折算"),
    "gpt-4o-mini": ModelPrice(1.1, 4.3, "0.15 / 0.6 USD，按 7.2 折算"),
    # ---- 向量化（按输入计费，无输出 token）----
    "text-embedding-3-small": ModelPrice(0.15, 0.0, "0.02 USD，按 7.2 折算"),
    "text-embedding-v3": ModelPrice(0.5, 0.0, "DashScope 通用向量"),
}

# 查不到单价的模型（例如自定义 provider 随便起的名字）按这个档位估：
# 取国内主流对话模型的中位价，宁可高估也不要让成本面板显示 0 ——
# 「显示 0」会让管理员误以为这个模型免费，从而放心地把流量切过去。
UNKNOWN_MODEL_PRICE = ModelPrice(2.0, 8.0, "未知模型兜底价（按 DeepSeek 标准价估）")


def normalize_model_name(model: Optional[str]) -> str:
    """把上游返回的模型名收敛成价目表的键

    上游常见三种写法：`deepseek-chat`、`deepseek/deepseek-chat`、
    `openai/gpt-4o-mini-2024-07-18`。统一去掉 vendor 前缀、转小写。
    """
    if not model:
        return ""
    name = str(model).strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name


def lookup_price(model: Optional[str]) -> Tuple[ModelPrice, bool]:
    """返回 (单价, 是否为已知模型)。

    最长前缀匹配：`deepseek-chat-0324` 命中 `deepseek-chat`，
    而不是被更短的键误匹配；也允许未来出现 `qwen-plus-2025` 这类带日期的名字。
    """
    name = normalize_model_name(model)
    if not name:
        return UNKNOWN_MODEL_PRICE, False

    best_key = ""
    for key in PRICE_TABLE:
        if name.startswith(key) and len(key) > len(best_key):
            best_key = key
    if best_key:
        return PRICE_TABLE[best_key], True
    return UNKNOWN_MODEL_PRICE, False


def compute_cost_cny(
    model: Optional[str], prompt_tokens: int, completion_tokens: int
) -> Tuple[float, bool]:
    """按单价算出这一次调用的花费（元），并告知是否用了兜底价。

    保留 6 位小数：单次调用常在 0.000x 元量级，四舍五入到 2 位会全变成 0，
    日汇总就再也对不上账。展示层再做格式化。
    """
    price, known = lookup_price(model)
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    cost = (
        prompt_tokens / 1_000_000 * price.input_cny
        + completion_tokens / 1_000_000 * price.output_cny
    )
    return round(cost, 6), known


def estimate_tokens(text: Optional[str]) -> int:
    """没有 usage 时的粗略估算（中英混排）。

    【什么时候会用到】上游没返回 usage 字段（部分自建网关、部分兼容实现），
    或者调用直接失败。此时**必须**留一个估算值并打上 `tokens_estimated` 标记 ——
    否则成本面板会把「不知道」显示成「0」，看起来像免费。

    系数：中文按 0.6 token/字（DeepSeek/Qwen 分词器的经验值），
    其余字符按 4 字符/token（英文 BPE 的常见比例）。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return max(1, int(math.ceil(cjk * 0.6 + other / 4)))


def flatten_payload(payload) -> str:
    """把 `llm.invoke` 的入参压成一段可用于估算 token 的文本。

    入参有三种形态：字符串、`HumanMessage` 列表、`(role, content)` 元组列表。
    只取 content，不带 role 前缀 —— 估算本就粗糙，再算上格式开销没有意义。
    """
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (list, tuple)):
        parts = []
        for item in payload:
            if isinstance(item, str):
                parts.append(item)
                continue
            content = getattr(item, "content", None)
            if content is None and isinstance(item, (list, tuple)) and len(item) >= 2:
                content = item[1]
            if isinstance(content, str):
                parts.append(content)
        return "\n".join(parts)
    content = getattr(payload, "content", None)
    return content if isinstance(content, str) else str(payload)


def extract_token_usage(result) -> Optional[Tuple[int, int, int]]:
    """从 LangChain 的返回对象里取出 (prompt, completion, total) token。

    两处来源，按可靠性排序：
    1. `usage_metadata`（LangChain ≥0.2 的标准字段，某些版本会缺 total）；
    2. `response_metadata["token_usage"]`（OpenAI 原始字段，DeepSeek/Qwen 兼容模式也给）。
    都取不到返回 None，由调用方退回字符估算。
    """
    meta = getattr(result, "usage_metadata", None)
    if isinstance(meta, dict) and (meta.get("input_tokens") or meta.get("output_tokens")):
        prompt = int(meta.get("input_tokens") or 0)
        completion = int(meta.get("output_tokens") or 0)
        total = int(meta.get("total_tokens") or (prompt + completion))
        return prompt, completion, total

    raw = getattr(result, "response_metadata", None)
    if isinstance(raw, dict):
        usage = raw.get("token_usage") or raw.get("usage")
        if isinstance(usage, dict):
            prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            completion = int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )
            total = int(usage.get("total_tokens") or (prompt + completion))
            if total:
                return prompt, completion, total
    return None


def model_of(llm) -> str:
    """读出客户端绑定的模型名（不同 LangChain 版本字段名不同）"""
    for attr in ("model_name", "model"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"
