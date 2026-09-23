# TechKnowledgeBase - 技术卡片知识库
"""
累积式技术卡片知识库：
- Qdrant 存卡片（向量 + payload，按分类标签归档，支持按分类过滤）
- Redis 存已抓取 item_id 集合（仅为加速缓存）

数据流:
抓取 -> 去重(collection_service) -> LLM 结构化摘要 -> 存入 Qdrant(累积) -> 随机/按分类读取

去重的**真相源**是 PostgreSQL 的 `collection_records.item_id`（阶段 3 内容溯源），
本模块的 `CRAWLED_SET` 只是它的镜像缓存；业务代码判断「抓过没有」请用
`src.services.collection_service.get_collected_ids()`，不要直接读 Redis。
"""

import hashlib
import os
import random
import threading
import time
from typing import List, Dict, Any, Optional

import redis
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchAny,
    MatchValue,
)

from loguru import logger

from src.core import circuit_breaker
from src.core.config import settings
from src.core.resilience import log_downstream_failure, with_retry_sync

# Qdrant 客户端的「瞬时」异常：ResponseHandlingException 是底层传输层的包装
# （httpx 连接失败 / 读超时都归到这里）。UnexpectedResponse 刻意不算 —— 那是
# 「查询本身有问题」的 4xx 类错误，重试不会变好。
QDRANT_TRANSIENT = (ResponseHandlingException, TimeoutError)

COLLECTION = "tech_encyclopedia"
VECTOR_SIZE = 1536
CRAWLED_SET = "crawled:item_ids"

_kb = None

# 单例构造锁：`get_knowledge_base()` 现在会被 `asyncio.to_thread` 从线程池并发调用
# （路由侧为了不阻塞事件循环而卸载），不再是「事件循环内天然串行」。
# 没有锁的话，并发首调会各自看到 `_kb is None`，构造出多个实例 ——
# 每个实例都会跑一次 `_init_collection()`（一次 Qdrant 往返），白付 N 次网络开销，
# 而且被丢弃的实例各自持有一份连接池。双重检查 + 锁把构造收敛成一次。
_kb_lock = threading.Lock()


def get_knowledge_base():
    """获取知识库单例（线程安全）

    注意首次调用会真的发网络请求：`TechKnowledgeBase.__init__` 里
    `_init_collection()` 会 `get_collections()`（最长 `qdrant_timeout`）。
    所以路由侧必须用 `asyncio.to_thread` 调用它，别在事件循环里直接调。
    """
    global _kb
    if _kb is None:
        with _kb_lock:
            if _kb is None:
                _kb = TechKnowledgeBase()
    return _kb


class TechKnowledgeBase:
    """技术卡片知识库（Qdrant + Redis）"""

    def __init__(self):
        # 客户端默认取**读档**（4s）：这个客户端绝大多数调用是 scroll / count 读操作；
        # 写操作（upsert）在调用点单独按写档放宽到 6s。
        self.qdrant = QdrantClient(url=settings.qdrant_url, timeout=settings.qdrant_timeout)
        # 走 settings 而不是 os.getenv：pydantic-settings 同时覆盖进程环境与 .env 文件，
        # 优先级也是「进程环境 > .env > 默认值」，是 os.getenv 的超集。
        self.redis = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            # 之前这里没配超时 = 无限等。Redis 不可达时整条链路会卡死，
            # 而它只是热缓存 —— 该降级就得能降下去。
            socket_timeout=settings.redis_socket_timeout,
            socket_connect_timeout=settings.redis_socket_timeout,
        )
        self._init_collection()

    def _init_collection(self):
        names = [c.name for c in self.qdrant.get_collections().collections]
        if COLLECTION not in names:
            self.qdrant.create_collection(
                collection_name=COLLECTION,
                vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
            )

    # ==================== 去重（Redis 镜像缓存） ====================

    def is_crawled(self, item_id) -> bool:
        """检查条目是否已抓取过（只读 Redis 缓存，不等同于真相源）

        阶段 3 起去重真相源是 `collection_records`，业务代码请改用
        `collection_service.get_collected_ids()`；这里保留给不依赖数据库的场景。
        """
        try:
            return bool(self.redis.sismember(CRAWLED_SET, str(item_id)))
        except Exception:
            return False

    def mark_crawled(self, item_id):
        """标记条目已抓取（只写 Redis 缓存，不落溯源表）

        正常采集链路请用 `collection_service.record_item()` 双写 DB + Redis。
        """
        try:
            self.redis.sadd(CRAWLED_SET, str(item_id))
        except Exception as e:
            print(f"[WARN] mark_crawled failed: {e}")

    # ==================== 向量 ====================

    async def _embed(self, text: str) -> Optional[List[float]]:
        """生成文本向量；无 Key 或失败时返回 None"""
        api_key = os.getenv("ALIYUN_API_KEY") or os.getenv("VOYAGE_API_KEY") or os.getenv("ZHIPU_API_KEY") or ""
        if not api_key:
            return None
        try:
            from src.modules.agent.embedding_service import get_embedding_service
            svc = get_embedding_service(api_key)
            return await svc.generate_embedding(text)
        except Exception as e:
            print(f"[WARN] embed failed: {e}")
            return None

    # ==================== 写入 ====================

    async def upsert(self, item: Dict[str, Any]):
        """存入一张技术卡片（累积式，按 id 覆盖同一张）"""
        # 向量化输入：把结构化字段拼成更丰富的检索文本，提高召回质量
        from src.modules.discovery.summary_spec import CATEGORY_LABELS

        title = item.get("title", "")
        category = item.get("category", "")
        category_zh = CATEGORY_LABELS.get(category) or category
        parts = [
            title,
            item.get("language") or item.get("source") or "",
            item.get("one_liner", ""),
            item.get("summary", ""),
            item.get("problem", ""),
        ]
        if item.get("tech_stack"):
            parts.append("技术栈：" + ", ".join(str(t) for t in item["tech_stack"]))
        if item.get("highlights"):
            parts.append("亮点：" + "; ".join(str(h) for h in item["highlights"]))
        if item.get("use_cases"):
            parts.append("场景：" + "; ".join(str(u) for u in item["use_cases"]))
        text = " ".join(p for p in parts if p)
        if category_zh and category_zh not in text:
            text = f"{category_zh}。{text}"
        vector = await self._embed(text)
        if vector is None or len(vector) != VECTOR_SIZE:
            if vector is not None:
                print(f"[WARN] 向量维度不匹配: {len(vector)} != {VECTOR_SIZE}，使用降级向量")
            vector = [0.1] * VECTOR_SIZE  # 紧急降级

        point_id = int(hashlib.md5(str(item["id"]).encode()).hexdigest()[:16], 16)
        # README 原文是留给 PostgreSQL 的内容真相源，不是给向量库过滤用的元数据：
        # 单条几十 KB，塞进 payload 会撑爆索引并拖慢 scroll，必须排除。
        payload = {
            k: v
            for k, v in item.items()
            if k not in {"vector", "raw_content", "content_meta"}
        }
        self.qdrant.upsert(
            collection_name=COLLECTION,
            points=[PointStruct(id=point_id, vector=vector, payload=payload)],
            timeout=settings.qdrant_write_timeout,
        )

    # ==================== 读取 ====================

    def count(self, item_type: str = None) -> int:
        """知识库卡片总数（可按 type 过滤：repo/article）"""
        try:
            count_filter = None
            if item_type:
                count_filter = Filter(must=[FieldCondition(key="type", match=MatchValue(value=item_type))])
            return self.qdrant.count(
                collection_name=COLLECTION,
                count_filter=count_filter,
                exact=True,
            ).count
        except Exception:
            return 0

    def _scroll_all(self, scroll_filter=None) -> List:
        """翻页拿全量 points（知识库规模下足够快）"""
        points = []
        offset = None
        while True:
            try:
                batch, offset = self.qdrant.scroll(
                    collection_name=COLLECTION,
                    scroll_filter=scroll_filter,
                    limit=500,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception as e:
                print(f"[WARN] scroll failed: {e}")
                break
            if not batch:
                break
            points.extend(batch)
            if offset is None:
                break
        return points

    def all_cards(self) -> List[Dict[str, Any]]:
        """全量卡片 payload（历史数据回填 / 数据体检等离线任务用）"""
        return [p.payload for p in self._scroll_all()]

    def sample(self, n: int, item_type: str = None) -> List[Dict[str, Any]]:
        """随机抽取 n 张卡片（可按 type 过滤：repo/article，用于「换一批」秒回）"""
        scroll_filter = None
        if item_type:
            scroll_filter = Filter(must=[FieldCondition(key="type", match=MatchValue(value=item_type))])
        points = self._scroll_all(scroll_filter)
        if not points:
            return []
        sample = random.sample(points, min(n, len(points)))
        return [p.payload for p in sample]

    def candidate_points(self, item_type: str = None, limit: int = 500) -> List[Dict[str, Any]]:
        """取**带向量**的候选集合：`[{"payload": {...}, "vector": [...]}]`

        与 `sample()` 的两个区别，都是个性化排序需要的：
        - **带向量**：排序靠余弦相似度，不取向量就没得算；
        - **不随机**：随机抽样会把「最相关的那张」直接抽掉，排序就失去意义。

        向量缺失的条目仍会返回（vector=None），由排序器统一按退化处理，
        这样「多少张没向量」在调用侧可见，不会静默消失。
        """
        scroll_filter = None
        if item_type:
            scroll_filter = Filter(must=[FieldCondition(key="type", match=MatchValue(value=item_type))])

        points = []
        offset = None
        while len(points) < limit:
            try:
                batch, offset = self.qdrant.scroll(
                    collection_name=COLLECTION,
                    scroll_filter=scroll_filter,
                    limit=min(500, limit - len(points)),
                    offset=offset,
                    with_payload=True,
                    with_vectors=True,
                )
            except Exception as e:
                print(f"[WARN] candidate_points failed: {e}")
                break
            if not batch:
                break
            points.extend(batch)
            if offset is None:
                break

        candidates: List[Dict[str, Any]] = []
        for p in points:
            vector = p.vector
            if isinstance(vector, dict):  # 命名向量配置下取第一个（本库是单向量）
                vector = next(iter(vector.values()), None)
            candidates.append(
                {
                    "payload": p.payload or {},
                    "vector": list(vector) if vector else None,
                }
            )
        return candidates

    def list_by_category(self, category: str, n: int = 50) -> List[Dict[str, Any]]:
        """按分类标签查询卡片（用于「技术百科」板块）"""
        try:
            points, _ = self.qdrant.scroll(
                collection_name=COLLECTION,
                scroll_filter=Filter(must=[FieldCondition(key="category", match=MatchValue(value=category))]),
                limit=n,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            print(f"[WARN] list_by_category failed: {e}")
            return []
        return [p.payload for p in points]

    def get_by_id(self, item_id: str) -> Optional[Dict[str, Any]]:
        """按卡片 id 查询（用于对话上下文恢复）"""
        try:
            points, _ = self.qdrant.scroll(
                collection_name=COLLECTION,
                scroll_filter=Filter(must=[FieldCondition(key="id", match=MatchValue(value=item_id))]),
                limit=1,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            print(f"[WARN] get_by_id failed: {e}")
            return None
        if points:
            return points[0].payload
        return None

    def get_vectors_by_ids(
        self, item_ids, deadline: Optional[float] = None
    ) -> Dict[str, List[float]]:
        """批量按 id 取**已存向量**，返回 {item_id: vector}。

        兴趣画像要复用卡片入库时的向量，而不是拿 summary 重新 embed：重新 embed 既
        多付一次 embedding 成本，又可能与入库向量不在同一个语义空间（换过模型）。
        与 get_by_ids 一样走批量 scroll，只是带上向量。

        无向量的条目直接不出现在结果里，由调用方当作「不可用」跳过。
        """
        ids = [str(i) for i in item_ids if i]
        if not ids:
            return {}
        # 熔断打开：Qdrant 已连续失败，画像直接当作「取不到向量」，
        # 由调用方回退随机抽样 —— 比逐个请求去撞一次快得多。
        if circuit_breaker.is_open(circuit_breaker.QDRANT_READ):
            print(
                "[WARN] event=circuit_open downstream=qdrant_read action=skip "
                "scope=interest_profile"
            )
            return {}

        _started = time.monotonic()
        try:
            # 画像取向量是「尽力而为」的个性化链路，但一次连接抖动就整批回退随机
            # 过于激进：重试 1 次（0.3s 退避）足够吸收连接重置这类瞬时故障。
            points, _ = with_retry_sync(
                lambda: self.qdrant.scroll(
                    collection_name=COLLECTION,
                    scroll_filter=Filter(must=[FieldCondition(key="id", match=MatchAny(any=ids))]),
                    limit=len(ids),
                    with_payload=True,
                    with_vectors=True,
                ),
                attempts=1,
                base_delay=0.3,
                retry_on=QDRANT_TRANSIENT,
                label="qdrant_read",
                deadline=deadline,
            )
        except Exception as e:
            circuit_breaker.record_failure(circuit_breaker.QDRANT_READ)
            log_downstream_failure(
                downstream="qdrant_read",
                exc=e,
                elapsed_ms=(time.monotonic() - _started) * 1000,
                scope="interest_profile",
            )
            return {}
        circuit_breaker.record_success(circuit_breaker.QDRANT_READ)

        vectors: Dict[str, List[float]] = {}
        for p in points:
            item_id = (p.payload or {}).get("id")
            vector = p.vector
            if isinstance(vector, dict):  # 命名向量配置下取第一个（本库是单向量）
                vector = next(iter(vector.values()), None)
            if item_id and vector:
                vectors[str(item_id)] = list(vector)
        return vectors

    def get_by_ids(self, item_ids) -> Dict[str, Dict[str, Any]]:
        """批量按 id 查询，返回 {item_id: payload}。

        收藏列表这类「一次要 N 张卡片」的场景必须走批量：逐条 get_by_id 会变成
        N 次 Qdrant 往返，列表一长就是几百次网络调用。
        """
        ids = [str(i) for i in item_ids if i]
        if not ids:
            return {}
        try:
            points, _ = self.qdrant.scroll(
                collection_name=COLLECTION,
                scroll_filter=Filter(must=[FieldCondition(key="id", match=MatchAny(any=ids))]),
                limit=len(ids),
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            print(f"[WARN] get_by_ids failed: {e}")
            return {}
        return {p.payload["id"]: p.payload for p in points if p.payload and p.payload.get("id")}
