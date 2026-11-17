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
from src.core.resilience import log_downstream_failure, qdrant_timeout_seconds, with_retry_sync

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
        # 候选池缓存：key = f"{item_type}:{limit}"，value = (过期时间 monotonic, 池)
        # 池里装的是 numpy 行归一化矩阵，写操作（upsert）会让它整体失效。
        self._pool_cache: Dict[str, Any] = {}
        self._pool_lock = threading.Lock()

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
            # 必须过 qdrant_timeout_seconds：qdrant-client 的 REST 客户端对**按次**
            # 传入的 timeout 做 `int(str(值))`，传 float 6.0 会直接 ValueError，
            # 请求发不出去。详见该助手的 docstring。
            timeout=qdrant_timeout_seconds(settings.qdrant_write_timeout),
        )
        # 新卡片进库 → 候选池的成员集合变了，必须整池作废。
        # 不是「往池里追加一行」：矩阵与 payloads 靠下标一一对应，追加时若中间
        # 任何一步失败，就会出现「A 的向量配 B 的卡片」—— 这是静默的错推荐，
        # 比多付一次 scroll 严重得多。
        self.invalidate_candidate_pool()

    def invalidate_candidate_pool(self) -> None:
        """让候选池缓存失效（写操作后调用）"""
        with self._pool_lock:
            self._pool_cache.clear()

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

    # ==================== 候选池（个性化排序的快速通道） ====================

    def _build_pool(self, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """把 `candidate_points` 的原始结果压成「payload 列表 + 行归一化矩阵」

        为什么值得在进程里留一份：这一步把 500×1536 个 float 从 Python 对象搬进
        连续内存（一次性 ~6MB），换来的是后续每次排序只需一次矩阵乘（~1ms），
        而不是每请求都重新 scroll + 装箱。代价是缓存必须能在数据变化时明确失效。
        """
        try:
            import numpy as np

            # 延迟导入：tech_knowledge 被 services/collection_service 依赖，
            # 模块级 import services.recommend 会绕回 services 包形成环。
            from src.services.recommend import DEGENERATE_STD
        except Exception as exc:  # noqa: BLE001 - 没有 numpy 就退回逐条排序
            logger.info(f"[INFO] 候选池跳过快通道（numpy 不可用）: {exc}")
            return None

        payloads: List[Dict[str, Any]] = []
        rows = []
        dropped = 0
        for candidate in candidates:
            vector = candidate.get("vector")
            if not vector or len(vector) != VECTOR_SIZE:
                dropped += 1
                continue
            payloads.append(candidate.get("payload") or {})
            rows.append(vector)
        if not rows:
            return None

        matrix = np.asarray(rows, dtype=np.float64)
        finite = np.isfinite(matrix).all(axis=1)
        norms = np.linalg.norm(matrix, axis=1)
        stds = matrix.std(axis=1)
        # 与 recommend.is_degenerate_vector 同一标准：常量向量（降级写入的 [0.1]*1536）
        # 与任何向量的余弦都相同，留着只会往排序结果里掺噪声
        keep = finite & (norms > 1e-9) & (stds >= DEGENERATE_STD)
        dropped += int((~keep).sum())
        if not keep.any():
            return None

        matrix = matrix[keep] / norms[keep][:, None]
        return {
            "payloads": [p for p, alive in zip(payloads, keep) if alive],
            "matrix": matrix,
            "dropped": dropped,
            "built_at": time.time(),
        }

    def candidate_pool(
        self, item_type: str = None, limit: int = 500
    ) -> Optional[Dict[str, Any]]:
        """候选池（带进程内缓存）：`{"payloads", "matrix", "dropped", "built_at"}`

        TTL 由 `settings.candidate_pool_ttl_seconds` 控制（<=0 表示不缓存）。
        缓存的意义是**把 Qdrant 带向量 scroll 摊到多次请求上**：一次 scroll 要拉
        500 条 × 1536 维向量，本机实测 ~450ms，而首页只取 3 张卡。

        一致性策略：写路径（`upsert`）调用 `invalidate_candidate_pool()` 整池作废，
        TTL 只是「万一有人绕过 upsert 直接写库」的保险。因此正常采集链路下，
        数据变化即时可见，不必等 TTL 过期。
        """
        key = f"{item_type or '*'}:{limit}"
        ttl = settings.candidate_pool_ttl_seconds
        now = time.monotonic()

        if ttl > 0:
            with self._pool_lock:
                entry = self._pool_cache.get(key)
                if entry and entry[0] > now:
                    return entry[1]

        pool = self._build_pool(self.candidate_points(item_type=item_type, limit=limit))

        if ttl > 0 and pool is not None:
            with self._pool_lock:
                self._pool_cache[key] = (time.monotonic() + ttl, pool)
        return pool

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

    def similar_items(
        self,
        item_id: str,
        item_type: Optional[str] = None,
        limit: int = 10,
        min_score: float = 0.0,
        deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        """按向量近邻找相似卡片，返回 `{"items": [...], "reason": str|None}`

        为什么走 Qdrant 原生检索而不是拿候选池自己算矩阵乘：
        - 候选池只取固定 500 条，库里超出部分**永远不可能**被召回；HNSW 索引则扫全库；
        - 候选池是「为了首页个性化排序」调的，把它的容量当作相似推荐的召回范围，
          等于让两个不相干的需求互相限制。

        三处必须做对的细节：

        1. **排除自身**。源卡片与自己的余弦恒为 1.0，不排除就永远占掉第一条。
        2. **同类型过滤**。仓库的「相似」应该是另一个仓库；把文章混进来的结果
           对「接着看哪个项目」没有任何帮助。
        3. **阈值在服务端生效**（`score_threshold`）。在客户端过滤等于先把
           limit 条捞满再丢掉一半，最终很可能只剩两三条 —— 让 Qdrant 直接
           按阈值召回，才能补足条数。

        Returns:
            `{"items": [{"payload": {...}, "score": float}], "reason": None}`
            取不到源向量或 Qdrant 不可用时 `items` 为空，`reason` 说明原因
            （`no_vector` / `unavailable`），由调用方决定是隐藏还是报错。
        """
        key = str(item_id)
        # 复用 get_vectors_by_ids：它已经带了熔断 + 一次瞬时故障重试，
        # 且保证取的是**入库时那份**向量（重新 embed 可能与库里的不在同一空间）
        vectors = self.get_vectors_by_ids([key], deadline=deadline)
        vector = vectors.get(key)
        if not vector:
            return {"items": [], "reason": "no_vector"}

        if circuit_breaker.is_open(circuit_breaker.QDRANT_READ):
            logger.warning(
                "[WARN] event=circuit_open downstream=qdrant_read action=skip scope=similar"
            )
            return {"items": [], "reason": "unavailable"}

        must = []
        if item_type:
            must.append(FieldCondition(key="type", match=MatchValue(value=item_type)))
        # 用 payload 里的 id 排除自身，而不是 HasIdCondition：point id 是
        # md5(id) 前 16 位的整数，这里重算一遍等于把「寻址方式」抄到第二个地方，
        # 一旦 upsert 侧改动就会静默失效
        must_not = [FieldCondition(key="id", match=MatchValue(value=key))]

        _started = time.monotonic()
        try:
            result = with_retry_sync(
                lambda: self.qdrant.query_points(
                    collection_name=COLLECTION,
                    query=vector,
                    query_filter=Filter(must=must or None, must_not=must_not),
                    limit=max(1, limit),
                    with_payload=True,
                    with_vectors=False,
                    score_threshold=min_score if min_score > 0 else None,
                    # 读路径用 qdrant_timeout（4s），不是写路径的 6s
                    timeout=qdrant_timeout_seconds(settings.qdrant_timeout),
                ),
                attempts=1,
                base_delay=0.3,
                retry_on=QDRANT_TRANSIENT,
                label="qdrant_read",
                deadline=deadline,
            )
        except Exception as e:  # noqa: BLE001 - 相似推荐是可选的增强，不该拖垮页面
            circuit_breaker.record_failure(circuit_breaker.QDRANT_READ)
            log_downstream_failure(
                downstream="qdrant_read",
                exc=e,
                elapsed_ms=(time.monotonic() - _started) * 1000,
                scope="similar",
            )
            return {"items": [], "reason": "unavailable"}
        circuit_breaker.record_success(circuit_breaker.QDRANT_READ)

        points = getattr(result, "points", None) or []
        items = [
            {"payload": p.payload or {}, "score": float(p.score)}
            for p in points
            if p.payload
        ]
        return {"items": items, "reason": None}

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
