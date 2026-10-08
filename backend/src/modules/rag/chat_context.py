# ChatContext - RAG 的最后一跳：把检索命中变成「可注入提示词的一段参考资料」
"""检索命中 → 回取正文 → 提示词里的【参考资料】段 + 可溯源的引用列表。

`retriever.py` 负责「找到」，`hydrate_texts()` 负责「取回正文」，本模块负责
**「怎么给模型看」** —— 这是 RAG 里唯一直接决定回答质量的一步，也是唯一
「做错了不会报错」的一步：

- **不注入** → 检索白做，问答还是纯靠会话上下文（这是接进来之前的状态）；
- **无预算地全量注入** → 5 条长块挤掉回答空间，模型反而更差；
- **取不到正文却照注入** → 模型只能照着「空的参考资料」编，比不给资料更糟。

## 三个设计决定

1. **查询就是用户原话，不做改写、不拼主题锚点**。锚点曾被认为能救「再举个例子」
   这类追问，实测也确实救活了（0.2538 → 0.5670）；但同一个机制让语料外的短问题
   从 0/4 误纳变成 **4/4 误纳** —— 主题短语与语料高度相似，拼进去后不管问什么都
   自带约 0.2 的「像」。**正确性 > 丰富度**，所以撤掉。完整数据见
   `build_retrieval_query()`。
2. **按 token 预算截断，而不是按条数**。`top_k=5` 只限制了条数，而块长本身
   可以从几十到两千字符 —— 条数封顶挡不住总量膨胀。
3. **只给「本轮依据了哪些块」，不给「哪句话来自哪块」**。句子级溯源要求模型
   逐句输出引用编号、再由我们回校验，代价是额外一轮解析且**不可靠**（模型会
   编出并不存在的编号）。给块级引用是诚实的：用户能看到依据，我们**不假装**
   做了做不到的事。

## 为什么正文取不到就丢弃，而不是留个占位

占位（如「[资料 3 暂不可用]」）既占掉一个名额，又让模型以为有三条资料可用。
丢弃后**重新编号**，模型看到的是「有几条就用几条」。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence, Tuple

from loguru import logger

from src.modules.rag.chunk_spec import estimate_tokens
from src.modules.rag.retriever import (
    DEFAULT_MIN_SCORE,
    DEFAULT_TOP_K,
    REASON_EMPTY_QUERY,
    HydratedChunk,
    hydrate_texts,
    search,
)

__all__ = [
    "MAX_CONTEXT_TOKENS",
    "REASON_NO_USABLE_TEXT",
    "REASON_HYDRATE_FAILED",
    "ChatReference",
    "ChatContext",
    "build_retrieval_query",
    "build_reference_block",
    "build_chat_context",
]


# 注入提示词的参考资料 token 预算。
#
# 取 1800 的依据：`retriever.DEFAULT_TOP_K` 的推导写着「5 条约 4,000 字符
# ≈ 2,000 token」，那是 8k 上下文里留够回答空间的经验值。1800 略低于它，
# 是为了给「对话历史 + 回答」留余量 —— 历史最多 10 条，本身也要吃几百 token。
#
# 超预算时**丢尾部命中**（保留分数最高的），不截断单块正文：半块正文很容易
# 把一句话截成两半，制造出「看起来有依据但读不通」的段落，比少给一条更糟。
MAX_CONTEXT_TOKENS = 1800

# 本模块特有的两个原因码（`retriever` 的原因码之外）。
REASON_HYDRATE_FAILED = "hydrate_failed"      # 取正文这一步整个失败
REASON_NO_USABLE_TEXT = "no_usable_text"      # 命中都有，但没有一条取得到正文


def build_retrieval_query(message: str) -> str:
    """规范化用户原话，作为检索查询。

    ## 为什么**不**把会话主题拼进来（一次被实测否掉的优化）

    最初的实现是：短问题（< 12 字符）拼上会话主题做锚点，理由是「再举个例子」
    这种追问单独检索必然拿不到东西。**方向是对的** —— 实测确实救活了追问：

        提问                      裸查       加锚点
        再举个例子                0.2538  →  0.5670    ✅ 从被挡变成过阈值
        它怎么裁剪视觉语言模型      0.5318  →  0.6654

    但同一个机制在**语料外的短问题**上把拒答闸门整个打穿了（阈值 0.35）：

        会被加锚点的 4 条负样本     裸查       锚「地震去噪」  锚「Scalpel」
        今天北京的天气怎么样        0.2279  →  0.4219        0.4271
        红烧肉怎么做才好吃          0.1345  →  0.4349        0.4858
        感冒了应该吃什么药          0.1384  →  0.3478        0.3755
        推荐几部好看的科幻电影      0.2251  →  0.4153        0.4993
        ─────────────────────────────────────────────────────────────
        误纳                       0/4     →      4/4

    原因很清楚：主题短语与语料**高度相似**，拼进去之后不管问什么都自带约 0.2 的
    「像」，分数被系统性抬过阈值。这不是调阈值能修的 —— 阈值调高会把真问题也拒掉。

    ## 权衡：不锚

    - 锚点的收益是「追问多拿到几段正文」—— 但会话上下文（主题 / 摘要 / 核心概念）
      **本来就已经注入提示词**了，追问仍然答得出来，只是不够具体；
    - 锚点的代价是「语料外的问题被注入 5 段无关正文」—— 模型会照着错料一本正经地编，
      这是**正确性**问题。

    **正确性 > 丰富度。** 追问检索要做对，得先有一份「追问评测集」把阈值重新标定，
    而不是拍一个更严的数字上去。列为待办，不在这里硬凑。

    复现上面两张表：`python rag_chat_probe.py --anchor-experiment`
    """
    return (message or "").strip()


@dataclass(frozen=True)
class ChatReference:
    """一条**块级**引用。字段是给前端和日志用的，不参与提示词拼接。"""

    ordinal: int                      # 提示词里的编号，从 1 开始，与块文本一致
    card_id: str
    title: str = ""
    source_url: str = ""
    score: float = 0.0
    heading_path: Tuple[str, ...] = ()
    excerpt: str = ""                 # 正文片段，仅供展示


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """按**估算** token 截断。二分查找，够用即可（估算本身就不精确）。"""
    if estimate_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def build_reference_block(
    chunks: Sequence[HydratedChunk],
    *,
    max_tokens: int = MAX_CONTEXT_TOKENS,
    excerpt_chars: int = 200,
) -> Tuple[str, Tuple[ChatReference, ...]]:
    """把带正文的命中拼成提示词里的【参考资料】段。

    Returns:
        `(块文本, 引用列表)`。块文本为空串表示**一条都没装进去**
        （全部取不到正文，或预算连第一条都放不下）。
    """
    if not chunks:
        return "", ()

    lines: list[str] = ["【参考资料】（来自项目知识库，按相关度从高到低）"]
    refs: list[ChatReference] = []
    used = 0

    for chunk in chunks:
        text = (chunk.text or "").strip()
        if not text:
            # 取不到正文的命中直接丢弃并**重新编号** —— 见模块 docstring。
            continue

        ordinal = len(refs) + 1
        title = chunk.hit.title or "（无标题）"
        entry = f"[{ordinal}] {title}\n{text}"
        cost = estimate_tokens(entry)

        if refs and used + cost > max_tokens:
            # 已经装了至少一条 → 停止累积，而不是截断这一条。
            break

        if not refs and cost > max_tokens:
            # 第一条就超预算（罕见：单块 2000 字符上限 × 韩文 1.75 ≈ 3500 token）。
            # 一条都不给等于检索白做，所以这里退化成截断 —— 全流程唯一的例外。
            entry = _truncate_to_tokens(entry, max_tokens)
            cost = estimate_tokens(entry)

        lines.append(entry)
        used += cost
        refs.append(
            ChatReference(
                ordinal=ordinal,
                card_id=chunk.hit.card_id,
                title=chunk.hit.title,
                source_url=chunk.hit.source_url,
                score=chunk.hit.score,
                heading_path=chunk.hit.heading_path,
                excerpt=text[:excerpt_chars],
            )
        )

    if not refs:
        return "", ()
    return "\n\n".join(lines), tuple(refs)


@dataclass(frozen=True)
class ChatContext:
    """问答侧一次检索的完整结果。

    `reason` 直接沿用 `retriever` 的原因码，另加本模块的两个。
    `degraded` 区分「系统原因没拿到」和「确实没有相关资料」——
    前者该告警，后者是正常业务（该走「我没有相关资料」的话术）。
    """

    query: str = ""
    block: str = ""
    references: Tuple[ChatReference, ...] = ()
    reason: str = ""
    degraded: bool = False

    @property
    def injected(self) -> bool:
        """是否真的往提示词里注入了参考资料"""
        return bool(self.block)


async def build_chat_context(
    message: str,
    *,
    deadline: Optional[float] = None,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    max_tokens: int = MAX_CONTEXT_TOKENS,
    embed_service: Optional[Any] = None,
    searcher: Callable[..., Any] = search,
    hydrator: Callable[..., Any] = hydrate_texts,
) -> ChatContext:
    """检索 → 回取正文 → 拼成可注入的一段。**任何失败都不抛异常。**

    检索是「锦上添花」的一环：它挂掉不该让整个问答 500，更不该让用户看到报错。
    失败一律转成 `ChatContext(reason=..., degraded=...)`，由调用方决定话术。

    Args:
        message: 用户本轮原话。**不做改写、不拼会话主题**，理由见
            `build_retrieval_query()` —— 那是一组实测数据，不是风格偏好。
        deadline: `time.monotonic()` 量纲的请求预算时间点。在线路径必须传。
        top_k / min_score: 透传给 `retriever.search`，默认与检索模块一致。
        max_tokens: 参考资料段的 token 预算。
        embed_service: 便于测试注入向量服务。
        searcher / hydrator: 便于测试注入；生产用默认值。

    Returns:
        `ChatContext`。`block` 为空串即「不注入」。
    """
    query = build_retrieval_query(message)
    if not query:
        return ChatContext(reason=REASON_EMPTY_QUERY)

    outcome = await searcher(
        query,
        top_k=top_k,
        min_score=min_score,
        deadline=deadline,
        embed_service=embed_service,
    )
    if not outcome.hits:
        return ChatContext(
            query=query, reason=outcome.reason, degraded=outcome.is_degraded
        )

    # 正文回 PostgreSQL 取，是同步 IO —— 必须整体丢线程池。
    try:
        chunks = await asyncio.to_thread(hydrator, outcome.hits)
    except Exception as exc:  # noqa: BLE001 - 取不到正文按「没有资料」降级
        logger.warning(
            "event=downstream_fallback downstream=postgres scope=chat_context "
            "reason=hydrate_failed hits={} error={}",
            len(outcome.hits),
            exc,
        )
        return ChatContext(query=query, reason=REASON_HYDRATE_FAILED, degraded=True)

    block, refs = build_reference_block(chunks, max_tokens=max_tokens)
    if not block:
        # 命中都在，但一条正文都没取到：这是数据问题（卡片被删 / 原文变短），
        # 不是「语料里没有」—— 所以算降级，要能告警出来。
        logger.warning(
            "event=downstream_fallback downstream=postgres scope=chat_context "
            "reason=no_usable_text hits={}",
            len(outcome.hits),
        )
        return ChatContext(query=query, reason=REASON_NO_USABLE_TEXT, degraded=True)

    return ChatContext(
        query=query,
        block=block,
        references=refs,
        reason=outcome.reason,
    )
