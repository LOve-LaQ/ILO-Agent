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
from typing import List, Dict, Any, Optional

import redis
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchAny,
    MatchValue,
)

COLLECTION = "tech_encyclopedia"
VECTOR_SIZE = 1536
CRAWLED_SET = "crawled:item_ids"

_kb = None


def get_knowledge_base():
    """获取知识库单例"""
    global _kb
    if _kb is None:
        _kb = TechKnowledgeBase()
    return _kb


class TechKnowledgeBase:
    """技术卡片知识库（Qdrant + Redis）"""

    def __init__(self):
        self.qdrant = QdrantClient(url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"))
        self.redis = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
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
        payload = {k: v for k, v in item.items() if k != "vector"}
        self.qdrant.upsert(
            collection_name=COLLECTION,
            points=[PointStruct(id=point_id, vector=vector, payload=payload)],
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
