# Chunk Store - 块级向量库（Qdrant `card_chunks` 集合）
"""块向量集合的读写层：建集合、批量写入、按卡删块、枚举已入库的卡。

与 `src/modules/discovery/tech_knowledge.py` 是**同一层**的东西（都是 Qdrant 的
门面），但刻意分成两个模块而不是往那边加方法：卡片集合和块集合的生命周期完全不同
—— 卡片是采集链路每次抓取都写，块是离线回填一次、只在换模型/改切分策略时重跑。
混在一个类里，块的那几个方法会永远挂在采集路径的依赖上。

## 为什么 payload 里不放正文

见 `chunk_spec.build_chunk_payload` 的说明。一句话：Qdrant 只负责「找到」，
正文的真相源始终是 PostgreSQL 的 `collection_records.raw_content`。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from loguru import logger
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)

from src.core.config import settings
from src.core.resilience import (
    log_downstream_failure,
    qdrant_timeout_seconds,
    with_retry_sync,
)
from src.modules.rag.chunk_spec import COLLECTION, VECTOR_SIZE, chunk_point_id

# 与 `tech_knowledge.QDRANT_TRANSIENT` 同一口径：ResponseHandlingException 是底层
# 传输层的包装（连接失败 / 读超时）。UnexpectedResponse 不算 —— 那是「请求本身
# 有问题」的 4xx 类错误，重试一万次结果一样。
QDRANT_TRANSIENT = (ResponseHandlingException, TimeoutError)

# 单次 upsert 的点数上限。
#
# 【为什么必须分批】1536 维 float32 约 6KB，加 payload 约 6.5KB。一次塞 13,468 点
# 就是约 88MB 的请求体 —— 服务端解析它要几十秒，客户端超时几乎必然，而且失败后
# 整批回滚，白跑。128 点 ≈ 0.83MB，是 Qdrant 的舒适区。
UPSERT_BATCH_SIZE = 128

# scroll 单页大小。只取 `card_id` 时每页很轻，500 是 qdrant 客户端的常用值。
_SCROLL_PAGE = 500

# MatchAny 单次能带的取值个数。Qdrant 对过滤条件里的取值列表没有硬上限，但
# 条件会随请求体一起序列化下发，几百个值就会让请求体明显变胖。分窗发更稳。
_MATCH_ANY_WINDOW = 256

_store: Optional["ChunkStore"] = None
_store_lock = threading.Lock()


def get_chunk_store() -> "ChunkStore":
    """块向量库单例（线程安全）

    与 `get_knowledge_base()` 同样的双重检查 + 锁：构造期会发一次 Qdrant 往返
    （`get_collections()`），并发首调不加锁会构造出多个实例，白付 N 次网络开销。
    """
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ChunkStore()
    return _store


def chunk_embedding_fingerprint() -> Optional[str]:
    """当前 embedding 的语义空间指纹（`provider:model:维度`）

    【为什么这里另写一份，而不复用 `interest_profile.embedding_fingerprint`】
    那个函数把维度写死成卡片集合的 `VECTOR_SIZE`。两个集合今天都是 1536，但一旦
    其中一边换了模型（维度跟着变），复用就会让两边的指纹一起漂 —— 指纹的全部价值
    就在于「模型一换它就变」，抄一个绑定到别的集合的值，正好把这条性质毁掉。

    漂的风险来自**抄常量**，不来自「读同一个单例」：这里同样从 EmbeddingService
    单例上取 provider / model，模型换了它必然跟着变。
    """
    api_key = (
        os.getenv("ALIYUN_API_KEY")
        or os.getenv("VOYAGE_API_KEY")
        or os.getenv("ZHIPU_API_KEY")
        or ""
    )
    if not api_key:
        return None
    try:
        from src.modules.agent.embedding_service import get_embedding_service

        service = get_embedding_service(api_key)
    except Exception as e:  # noqa: BLE001 - 指纹取不到就当作「不入库」，不能静默用错空间
        logger.warning(f"[WARN] 无法确定块 embedding 指纹: {e}")
        return None

    provider = getattr(service, "_current_provider", "unknown")
    model = getattr(service, f"{provider}_model", "unknown")
    return f"{provider}:{model}:{VECTOR_SIZE}"


class ChunkStore:
    """`card_chunks` 集合的门面"""

    def __init__(self, client: Optional[QdrantClient] = None):
        # 客户端默认取**读档**（4s）；写操作在调用点按写档（6s）放宽 —— 与
        # tech_knowledge 同一策略，理由也一样：写被掐断要重算向量，比读慢更贵。
        self.qdrant = client or QdrantClient(
            url=settings.qdrant_url, timeout=settings.qdrant_timeout
        )
        self._init_collection()

    # ==================== 建集合 ====================

    def _init_collection(self) -> None:
        """确保集合存在。

        【为什么这里不像 tech_knowledge 那样裸调】那张表是应用启动/采集链路的
        依赖，Qdrant 挂了整条链路本来就走不通；而块库是离线回填 + 检索增强，
        挂了应该给出「Qdrant 没起来」这种能直接照做的提示，而不是一个
        `ResponseHandlingException` 的堆栈。
        """
        try:
            names = [c.name for c in self.qdrant.get_collections().collections]
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"无法连接 Qdrant（{settings.qdrant_url}）：{e}。"
                "请确认 Qdrant 已启动（docker compose up -d qdrant）。"
            ) from e

        if COLLECTION in names:
            return
        self.qdrant.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        logger.info(f"[INFO] 已创建向量集合 {COLLECTION}（{VECTOR_SIZE} 维 / COSINE）")

    # ==================== 写入 ====================

    def _upsert_one_batch(self, batch: Sequence[PointStruct]) -> None:
        """单批 upsert，带一次瞬时故障重试。

        【为什么这里不挂熔断】熔断器是给**在线请求路径**用的：它记的是「这个下游
        对用户请求还可用吗」。块库的写入只发生在离线回填脚本里，一次 Qdrant 抖动
        应该让脚本重试/退出，而不是把全局的 `QDRANT_READ` 状态改成 OPEN 去影响
        线上读。检索路径（P3）会读这个熔断器，写入路径不碰。
        """
        started = time.monotonic()
        try:
            with_retry_sync(
                lambda: self.qdrant.upsert(
                    collection_name=COLLECTION,
                    points=list(batch),
                    # 必须过 qdrant_timeout_seconds：qdrant-client 的 REST 客户端对
                    # 按次传入的 timeout 做 `int(str(值))`，传 float 会直接 ValueError，
                    # 请求根本发不出去。详见该助手的 docstring。
                    timeout=qdrant_timeout_seconds(settings.qdrant_write_timeout),
                ),
                attempts=1,
                base_delay=0.5,
                retry_on=QDRANT_TRANSIENT,
                label="qdrant_write",
            )
        except Exception as exc:
            log_downstream_failure(
                downstream="qdrant_write",
                exc=exc,
                elapsed_ms=(time.monotonic() - started) * 1000,
                scope="chunk_store",
            )
            raise

    def upsert_chunks(self, items: Sequence[Dict[str, Any]]) -> int:
        """批量写入块，返回实际写入的点数。

        Args:
            items: 每项形如
                `{"card_id": str, "ordinal": int, "vector": list[float], "payload": dict}`

        【为什么入参不是 PointStruct】把 qdrant 的模型类型挡在门面里，调用方
        （回填脚本）就不需要知道「点 ID 怎么算」「用哪个集合」这些事 —— 那些
        恰好是最容易写错、且写错不报错的地方。
        """
        points: List[PointStruct] = []
        for item in items:
            card_id = str(item["card_id"])
            ordinal = int(item["ordinal"])
            vector = item["vector"]
            if not vector or len(vector) != VECTOR_SIZE:
                # 静默写入维度不对的向量会让整个集合不可用（Qdrant 会拒绝，
                # 但错误信息是「维度不匹配」而不是「哪张卡的哪个块」）。
                raise ValueError(
                    f"向量维度不匹配：card_id={card_id} ordinal={ordinal} "
                    f"got={len(vector) if vector else 0} expected={VECTOR_SIZE}"
                )
            points.append(
                PointStruct(
                    id=chunk_point_id(card_id, ordinal),
                    vector=list(vector),
                    payload=dict(item.get("payload") or {}),
                )
            )

        written = 0
        for start in range(0, len(points), UPSERT_BATCH_SIZE):
            batch = points[start : start + UPSERT_BATCH_SIZE]
            self._upsert_one_batch(batch)
            written += len(batch)
        return written

    def delete_card(self, card_id: str) -> None:
        """删掉某张卡的全部块。

        【为什么重切必须删】点 ID 是 `(card_id, ordinal)` 的哈希，新切分产生的
        第 0..N 块会覆盖旧的第 0..N 块，但**旧的第 N+1..M 块会原样留着** ——
        它们指向的 `char_start/char_end` 在新切分下已经指到别的文字上。检索命中
        这些孤儿块，会拿到一段和向量语义对不上的正文，且完全不报错。
        """
        started = time.monotonic()
        try:
            with_retry_sync(
                lambda: self.qdrant.delete(
                    collection_name=COLLECTION,
                    points_selector=FilterSelector(
                        filter=Filter(
                            must=[
                                FieldCondition(
                                    key="card_id", match=MatchValue(value=card_id)
                                )
                            ]
                        )
                    ),
                    timeout=qdrant_timeout_seconds(settings.qdrant_write_timeout),
                ),
                attempts=1,
                base_delay=0.5,
                retry_on=QDRANT_TRANSIENT,
                label="qdrant_delete",
            )
        except Exception as exc:
            log_downstream_failure(
                downstream="qdrant_delete",
                exc=exc,
                elapsed_ms=(time.monotonic() - started) * 1000,
                scope="chunk_store",
            )
            raise

    # ==================== 读取 ====================

    def count(self) -> int:
        """集合里的块总数"""
        try:
            return self.qdrant.count(collection_name=COLLECTION, exact=True).count
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WARN] 统计块数失败: {e}")
            return 0

    def _scroll_card_ids(self, scroll_filter: Optional[Filter] = None) -> Set[str]:
        """翻页取 payload 里的 `card_id`，去重后返回。

        只取 `card_id` 一个字段（`with_payload=["card_id"]`）而不是整个 payload：
        后者的 `heading_path` 等字段会让每页的响应体大好几倍，而我们只关心
        「这张卡在不在库里」。
        """
        found: Set[str] = set()
        offset = None
        while True:
            batch, offset = self.qdrant.scroll(
                collection_name=COLLECTION,
                scroll_filter=scroll_filter,
                limit=_SCROLL_PAGE,
                offset=offset,
                with_payload=["card_id"],
                with_vectors=False,
                timeout=qdrant_timeout_seconds(settings.qdrant_timeout),
            )
            if not batch:
                break
            for point in batch:
                card_id = (point.payload or {}).get("card_id")
                if card_id:
                    found.add(str(card_id))
            if offset is None:
                break
        return found

    def indexed_card_ids(self, card_ids: Sequence[str]) -> Set[str]:
        """给定候选卡 ID，返回其中**已经在库里**的那些。

        回填脚本靠它做幂等：已入库的卡默认跳过，不再付一次向量化费用。

        【为什么要分窗】`MatchAny` 的取值列表会随请求体一起下发，一次带上近千个
        卡 ID 会让请求体明显变胖。分窗到 `_MATCH_ANY_WINDOW` 更稳，代价只是多几次
        往返 —— 相对「白算一遍向量」可以忽略。
        """
        ids = [str(i) for i in card_ids if i]
        if not ids:
            return set()

        found: Set[str] = set()
        for start in range(0, len(ids), _MATCH_ANY_WINDOW):
            window = ids[start : start + _MATCH_ANY_WINDOW]
            found |= self._scroll_card_ids(
                Filter(must=[FieldCondition(key="card_id", match=MatchAny(any=window))])
            )
        return found

    def all_indexed_card_ids(self) -> Set[str]:
        """库里全部已索引的卡 ID（数据体检用）"""
        return self._scroll_card_ids()

    def close(self) -> None:
        try:
            self.qdrant.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响结果
            pass


def reset_store() -> None:
    """测试用：丢弃单例"""
    global _store
    _store = None
