# Retriever - 块级检索（查询侧）
"""把「用户的问题」变成「几张卡的若干段正文」。

这是检索增强生成的**查询半段第一跳**：问题 → 查询向量 → 块向量库 top-k → 候选块。
拼提示词与引用溯源属 P4，本模块只负责「找到」，并把**「为什么没找到」**一起返回。

## 与 `chunk_store` 的分工

| | 写侧（`chunk_store.py`） | 读侧（本模块） |
|---|---|---|
| 触发时机 | 离线回填脚本 | 在线问答请求 |
| 熔断 | **刻意不挂**（离线无在线用户，失败重跑即可） | **挂 `QDRANT_READ`**（保护在线延迟） |
| 超时 | 固定的写档 6s | 接请求级 deadline 夹取 |
| 失败时 | 抛异常让脚本退出 | 返回结构化空结果让上层降级 |

## 六个必须做对的地方（都是「做错了不报错」的类型）

1. **查询侧必须传 `text_type="query"`**。向量模型是**非对称**的：文档侧和查询侧走
   不同编码。传错侧别**不报错**，只是排序悄悄变差 —— 实测同一个问题，查询侧
   0.5792 / 文档侧 0.5464。这类错误没有任何日志会提示你。
2. **阈值拒答要后置过滤，不能传给 Qdrant 的 `score_threshold`**。理由见 `search()`。
3. **熔断检查必须在 embedding 之前**。Qdrant 已连续失败时先去算查询向量是白花钱
   —— 算完照样搜不了。
4. **Qdrant 调用必须整体丢进线程池**，且**包含 `get_chunk_store()` 本身**
   （它的构造期有一次 `get_collections()` 往返）。这条有 AST 守卫钉着
   （`tests/test_async_no_blocking_io.py`）。
5. **正文回 PostgreSQL 取，并且要防「索引之后原文被改过」**。见 `hydrate_texts()`。
6. **一条都没命中时也要返回「最高分是多少」**。否则「库里没有相关内容」和
   「有但不够像」在观测上分不开，而这两件事的处置完全不同（前者该去补语料，
   后者该调阈值或换模型）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger
from qdrant_client.http.exceptions import ResponseHandlingException
from sqlalchemy import select

from src.core import circuit_breaker
from src.core.config import settings
from src.core.db import get_session_factory
from src.core.deadline import clamp_timeout, is_expired
from src.core.resilience import (
    log_downstream_failure,
    qdrant_timeout_seconds,
    with_retry_sync,
)
from src.modules.rag.chunk_spec import COLLECTION
from src.modules.rag.chunk_store import get_chunk_store

# 与 chunk_store 同一口径：传输层异常才值得重试，4xx 类（UnexpectedResponse）
# 重试一万次结果一样。
QDRANT_TRANSIENT = (ResponseHandlingException, TimeoutError)

# 默认取几条。5 是「够用且不撑爆提示词」的经验值：块长中位数 774 字符，
# 5 条约 4,000 字符 ≈ 2,000 token，在 8k 上下文里留足了回答空间。
# 真正该取几条由评测集定（P3 后半），这里是**初始值**，不要当成结论。
DEFAULT_TOP_K = 5

# 单次检索的条数上限。存在的意义不是「Qdrant 撑不住」，而是防止调用方
# 传一个 `top_k=10000` 把整库拉回来 —— 那是把向量库当全表扫描用。
MAX_TOP_K = 50

# 拒答阈值。**这是从实测分布里挑的，不是拍脑袋的**：
#
#   有答案的问题（怎么裁剪视觉语言模型 / Scalpel / QQ 接 DeepSeek）：0.5792 ~ 0.6840
#   泛化问题（怎么安装 / 支持哪些大模型）：0.4808 ~ 0.4840
#   无答案的问题（今天天气 / 红烧肉怎么做 / 感冒了怎么办）：0.1313 ~ 0.1990
#
# 中间有一段 0.28 宽的**空带**，所以阈值只要落在这段里，效果几乎一样。
# 取 0.35（偏下沿）是**刻意偏向召回**：
#   - 阈值定高 → 本来能答的问题被拒 → 用户直接看到「没找到」，可见且恼人；
#   - 阈值定低 → 多塞一条边缘相关的块 → 只是多花几百 token，而且提示词层
#     （P4）本来就会要求模型「资料里没有就直说」。
# 也就是说：**阈值是成本过滤器，不是防幻觉机制**。防幻觉靠提示词和评测，
# 不靠相似度数字 —— 相似度高不等于内容能支撑答案。
DEFAULT_MIN_SCORE = 0.35

# 检索结果的原因码。**必须区分到这一层**，因为「空结果」有好几种成因，
# 而它们该触发的动作完全不同（见模块 docstring 第 6 条）。
REASON_OK = "ok"                        # 有块过了阈值
REASON_NO_HITS = "no_hits"              # 库里一条都没返回 —— 语料问题
REASON_BELOW_THRESHOLD = "below_threshold"  # 有返回但都不够像 —— 阈值/模型问题
REASON_CIRCUIT_OPEN = "circuit_open"    # Qdrant 连续失败，主动跳过
REASON_BUDGET_EXHAUSTED = "budget_exhausted"  # 请求预算已耗尽，未发起调用
REASON_EMBEDDING_FAILED = "embedding_failed"  # 查询向量算不出来
REASON_QDRANT_ERROR = "qdrant_error"    # Qdrant 调用抛异常
REASON_EMPTY_QUERY = "empty_query"      # 问题本身是空的

# 这几个原因下**调用方应当走「我没有相关资料」的降级话术**，而不是当作系统故障。
# 放在模块里而不是让调用方自己判断，是因为判断依据（哪些算「没有」、哪些算
# 「坏了」）属于检索层，调用方只该看到结论。
_NO_ANSWER_REASONS = frozenset(
    {REASON_NO_HITS, REASON_BELOW_THRESHOLD, REASON_EMPTY_QUERY}
)


@dataclass(frozen=True)
class ChunkHit:
    """一条命中。**只有坐标，没有正文** —— 正文的真相源在 PostgreSQL。

    `char_start` / `char_end` 是**原文的字符区间**（不是 token、不是字节），
    与 `chunker.py` 的区间不变式同一坐标系：`text == 原文[char_start:char_end]`。
    这个不变式是评测集能锚定字符区间（而不是块编号）的前提 —— 块编号会随切分
    参数变，字符区间不会。
    """

    card_id: str
    ordinal: int
    score: float
    heading_path: Tuple[str, ...] = ()
    char_start: int = 0
    char_end: int = 0
    title: str = ""
    source_url: str = ""


@dataclass(frozen=True)
class RetrievalOutcome:
    """一次检索的完整结果 —— 包括**没命中时的原因**。

    `hits` 只包含**过了阈值**的块；`best_score` 是过滤前的最高分。
    两者分开是为了让「有返回但被阈值挡掉」这件事可观测（见模块 docstring 第 6 条）。
    """

    query: str
    hits: Tuple[ChunkHit, ...] = ()
    best_score: float = 0.0
    # 默认取 `no_hits` 而不是 `ok`：手工构造时最可能的情况就是「什么都没找到」，
    # 默认成 `ok` 会让一个空结果自称成功 —— 而 `reason` 正是给调用方判断
    # 「该走降级话术还是该报故障」用的，默认值不能有歧义。
    # `search()` 的每个返回点都会显式传这个字段。
    reason: str = REASON_NO_HITS
    elapsed_ms: float = 0.0
    # 过滤前的原始命中数（含被阈值挡掉的）。用于区分
    # 「Qdrant 返回了 5 条但都不过阈值」和「Qdrant 一条都没返回」。
    raw_hits: int = 0

    @property
    def answered(self) -> bool:
        """是否拿到了可用的资料"""
        return bool(self.hits)

    @property
    def is_degraded(self) -> bool:
        """是否因为**系统原因**（而非「确实没有资料」）降级。

        这个区分决定了告警口径：`no_hits` 频繁说明语料不够，是产品问题；
        `circuit_open` / `qdrant_error` 频繁说明下游挂了，是运维问题。
        """
        return self.reason not in _NO_ANSWER_REASONS and self.reason != REASON_OK


# ==================== 同步检索内核 ====================


def _to_hit(point: Any) -> Optional[ChunkHit]:
    """把 Qdrant 的返回点转成 `ChunkHit`；payload 缺关键字段时返回 None。

    【为什么缺字段要丢弃而不是填默认值】`card_id` 和 `char_start/char_end` 是
    回取正文的唯一坐标。缺任何一个，这条命中都取不到正文，留着只会在结果里
    占一个名额、让调用方以为「找到了 5 条」实际只有 3 条可用。
    """
    payload = point.payload or {}
    card_id = payload.get("card_id")
    if not card_id:
        return None
    return ChunkHit(
        card_id=str(card_id),
        ordinal=int(payload.get("ordinal", 0)),
        score=float(getattr(point, "score", 0.0) or 0.0),
        heading_path=tuple(payload.get("heading_path") or ()),
        char_start=int(payload.get("char_start", 0)),
        char_end=int(payload.get("char_end", 0)),
        title=str(payload.get("title") or ""),
        source_url=str(payload.get("source_url") or ""),
    )


def _query_points_sync(
    query_vector: Sequence[float],
    top_k: int,
    timeout_s: float,
    deadline: Optional[float],
    collection: str,
) -> List[ChunkHit]:
    """**同步**检索内核 —— 调用方必须整体丢进线程池。

    【为什么单例构造也在这里面】`get_chunk_store()` 首次构造会发一次
    `get_collections()` 往返。把它留在 async 上下文里，等于给每个「首次检索」
    塞一次同步网络调用 —— 正是 AST 守卫要抓的那类。

    【为什么 `collection` 是参数而不是写死 `COLLECTION`】检索评测要做
    「按标题切 vs 固定长度」的对照实验，两套切分策略的块必须落在**两个集合**里
    （同一集合混装两种切分会产生「孤儿块」，见 `chunk_store.delete_card`）。
    默认值仍是 `COLLECTION`，在线路径不受影响。
    """
    store = get_chunk_store()
    response = with_retry_sync(
        lambda: store.qdrant.query_points(
            collection_name=collection,
            query=list(query_vector),
            limit=top_k,
            with_payload=True,
            # 不取向量：1536 维 float32 约 6KB/条，而检索结果只需要坐标和分数。
            with_vectors=False,
            # 【为什么不传 score_threshold】Qdrant 的 score_threshold 会在**服务端**
            # 过滤，看起来更省 —— 但那样「返回 0 条」就同时代表「库里没有」和
            # 「有但都没过阈值」，观测上分不开。我们在客户端后置过滤，多传几条的
            # 代价是每条约 200 字节 payload，换回的是可诊断性。
            timeout=qdrant_timeout_seconds(timeout_s),
        ),
        attempts=1,
        base_delay=0.3,
        retry_on=QDRANT_TRANSIENT,
        label="qdrant_chunk_search",
        deadline=deadline,
    )
    hits: List[ChunkHit] = []
    for point in getattr(response, "points", None) or []:
        hit = _to_hit(point)
        if hit is not None:
            hits.append(hit)
    return hits


# ==================== 查询入口 ====================


async def search(
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    deadline: Optional[float] = None,
    embed_service: Optional[Any] = None,
    collection: str = COLLECTION,
) -> RetrievalOutcome:
    """检索与问题相关的块。

    Args:
        query: 用户的问题（原样送进向量模型，**不做改写** —— 改写是独立的一步，
            要单独评测，现在还没有数据支撑「改写一定更好」）。
        top_k: 取几条候选，会被夹到 `[1, MAX_TOP_K]`。
        min_score: 拒答阈值，见 `DEFAULT_MIN_SCORE` 的推导。
        deadline: `time.monotonic()` 量纲的请求预算时间点。**传 None 表示不设预算**，
            适合离线脚本；在线路径必须传，否则一次慢检索会拖垮整个请求。
        embed_service: 便于测试注入。默认从单例取。
        collection: 查哪个集合。默认 `card_chunks`；评测脚本用它对比不同切分策略。

    Returns:
        `RetrievalOutcome`。**任何失败都不抛异常** —— 检索是「锦上添花」的一环，
        它挂掉不该让整个问答 500。失败通过 `reason` 表达。
    """
    started = time.monotonic()
    query = (query or "").strip()
    if not query:
        return RetrievalOutcome(query=query, reason=REASON_EMPTY_QUERY)

    top_k = max(1, min(int(top_k), MAX_TOP_K))

    # ---- 闸门 1：预算 ----
    # 放在最前面：预算已耗尽时连「检查熔断」都不必做，直接降级。
    if is_expired(deadline):
        return RetrievalOutcome(query=query, reason=REASON_BUDGET_EXHAUSTED)

    # ---- 闸门 2：熔断（**必须在 embedding 之前**）----
    # Qdrant 已经连续失败时，先算查询向量是白花钱 —— 算完照样搜不了。
    # 这个顺序看着是小事，但它是「检索挂掉时不要额外烧钱」的唯一保障：
    # 熔断打开期间每个请求都会走这条路径，而 embedding 是**按量计费**的。
    if circuit_breaker.is_open(circuit_breaker.QDRANT_READ):
        logger.warning(
            "event=circuit_open downstream=qdrant_read action=skip scope=chunk_retriever"
        )
        return RetrievalOutcome(query=query, reason=REASON_CIRCUIT_OPEN)

    # ---- 第一步：查询向量 ----
    # text_type="query" 是**非对称检索**的关键，见模块 docstring 第 1 条。
    # 传成 "document" 不会报错，只会让排序悄悄变差 —— 这是本模块最容易犯的错。
    service = embed_service
    if service is None:
        from src.modules.agent.embedding_service import get_embedding_service

        api_key = _embedding_api_key()
        if not api_key:
            return RetrievalOutcome(query=query, reason=REASON_EMBEDDING_FAILED)
        service = get_embedding_service(api_key)

    try:
        vector = await service.generate_embedding(query, text_type="query")
    except Exception as exc:  # noqa: BLE001 - 向量算不出来按「没有资料」降级
        log_downstream_failure(
            downstream="embedding",
            exc=exc,
            elapsed_ms=(time.monotonic() - started) * 1000,
            scope="chunk_retriever",
        )
        return RetrievalOutcome(query=query, reason=REASON_EMBEDDING_FAILED)

    if not vector:
        return RetrievalOutcome(query=query, reason=REASON_EMBEDDING_FAILED)

    # ---- 闸门 3：预算复查 ----
    # embedding 是一次真实的网络往返，可能用掉几百毫秒。不复查的话，
    # 预算刚好在 embedding 期间耗尽时，我们仍会发起一次注定超时的 Qdrant 调用。
    if is_expired(deadline):
        return RetrievalOutcome(
            query=query,
            reason=REASON_BUDGET_EXHAUSTED,
            elapsed_ms=(time.monotonic() - started) * 1000,
        )

    # ---- 第二步：向量检索（同步内核丢线程池）----
    timeout_s = clamp_timeout(settings.qdrant_timeout, deadline)
    try:
        hits = await asyncio.to_thread(
            _query_points_sync, vector, top_k, timeout_s, deadline, collection
        )
    except Exception as exc:  # noqa: BLE001 - 检索失败不抛，转成结构化降级
        circuit_breaker.record_failure(circuit_breaker.QDRANT_READ)
        log_downstream_failure(
            downstream="qdrant_read",
            exc=exc,
            elapsed_ms=(time.monotonic() - started) * 1000,
            timeout_s=timeout_s,
            scope="chunk_retriever",
        )
        return RetrievalOutcome(
            query=query,
            reason=REASON_QDRANT_ERROR,
            elapsed_ms=(time.monotonic() - started) * 1000,
        )

    circuit_breaker.record_success(circuit_breaker.QDRANT_READ)

    # ---- 第三步：后置阈值过滤 ----
    elapsed_ms = (time.monotonic() - started) * 1000
    if not hits:
        return RetrievalOutcome(
            query=query, reason=REASON_NO_HITS, elapsed_ms=elapsed_ms
        )

    best = max(h.score for h in hits)
    passed = tuple(h for h in hits if h.score >= min_score)
    if not passed:
        return RetrievalOutcome(
            query=query,
            best_score=best,
            reason=REASON_BELOW_THRESHOLD,
            elapsed_ms=elapsed_ms,
            raw_hits=len(hits),
        )

    return RetrievalOutcome(
        query=query,
        hits=passed,
        best_score=best,
        reason=REASON_OK,
        elapsed_ms=elapsed_ms,
        raw_hits=len(hits),
    )


def _embedding_api_key() -> str:
    """按 EmbeddingService 的优先级取 Key（与 chunk_store / tech_knowledge 一致）"""
    import os

    return (
        os.getenv("ALIYUN_API_KEY")
        or os.getenv("VOYAGE_API_KEY")
        or os.getenv("ZHIPU_API_KEY")
        or os.getenv("BAIDU_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    )


# ==================== 正文回填 ====================


@dataclass
class HydratedChunk:
    """命中 + 正文。`text` 为 None 表示**取不到正文**（见 `hydrate_texts`）。"""

    hit: ChunkHit
    text: Optional[str] = None
    problem: str = ""


def hydrate_texts(
    hits: Sequence[ChunkHit],
    *,
    max_chars_per_hit: int = 0,
) -> List[HydratedChunk]:
    """按 `card_id` + 字符区间回 PostgreSQL 取正文。**同步函数**（调用方按需卸载）。

    【为什么正文不在向量库里】见 `chunk_spec.build_chunk_payload`：单块上千字符，
    13,468 个块塞进 payload 会撑爆索引并拖慢 scroll。Qdrant 只负责「找到」。

    【为什么这里要防「原文被改过」】区间不变式 `text == 原文[start:end]` 只在
    **索引之后原文没变**时成立。README 是会变的 —— 采集链路每轮都会重新抓，
    `collection_records.raw_content` 会被覆盖。原文一变，同一对偏移量指到的就是
    别的文字，而且**不报任何错**：检索说「找到相关内容」，注入的却是无关段落。

    当前能查出来的只有「偏移量越界」这一种（原文变短了）。**更严的校验需要
    在 payload 里存块正文的哈希**，那样才能识别「长度没变但内容换了」——
    这是一次 schema 变更 + 重跑回填（约 ¥3.6 / 13 分钟），列为后续项。

    Args:
        hits: 检索命中
        max_chars_per_hit: 单块最多取多少字符（0 = 不截）。提示词层要控总量时用。

    Returns:
        与 `hits` **等长同序**的列表。取不到正文的项 `text is None`，
        由调用方决定是丢弃还是保留（**默认建议丢弃** —— 没有正文的命中，
        塞进提示词只能让模型编）。
    """
    if not hits:
        return []

    card_ids = sorted({h.card_id for h in hits})
    raw_by_card: Dict[str, str] = {}
    try:
        from src.models.collection import CollectionRecord

        stmt = select(CollectionRecord.item_id, CollectionRecord.raw_content).where(
            CollectionRecord.item_id.in_(card_ids)
        )
        with get_session_factory()() as session:
            for item_id, raw_content in session.execute(stmt).all():
                raw_by_card[str(item_id)] = raw_content or ""
    except Exception as exc:  # noqa: BLE001 - 取不到正文就整批标 problem，不抛
        logger.warning(
            "event=downstream_fallback downstream=postgres scope=chunk_hydrate reason={} error={}",
            "invoke_failed",
            exc,
        )
        return [
            HydratedChunk(hit=h, text=None, problem="postgres_unavailable") for h in hits
        ]

    out: List[HydratedChunk] = []
    for hit in hits:
        raw = raw_by_card.get(hit.card_id)
        if raw is None:
            out.append(HydratedChunk(hit=hit, text=None, problem="card_missing"))
            continue
        if hit.char_end > len(raw) or hit.char_start < 0 or hit.char_end <= hit.char_start:
            # 原文变短（或偏移量本身异常）→ 偏移量已不可信，**不能**截断凑合。
            # 截断会得到一段「看起来正常但实际错位」的文字，比取不到更危险。
            logger.warning(
                "event=chunk_span_invalid card_id={} ordinal={} span=[{},{}] len={}",
                hit.card_id,
                hit.ordinal,
                hit.char_start,
                hit.char_end,
                len(raw),
            )
            out.append(HydratedChunk(hit=hit, text=None, problem="span_out_of_range"))
            continue

        text = raw[hit.char_start : hit.char_end]
        if max_chars_per_hit > 0 and len(text) > max_chars_per_hit:
            text = text[:max_chars_per_hit]
        out.append(HydratedChunk(hit=hit, text=text))
    return out
