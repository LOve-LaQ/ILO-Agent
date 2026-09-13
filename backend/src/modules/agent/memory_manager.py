# MemoryManager - 记忆管理系统
"""
记忆管理模块 - 分层存储架构

功能:
1. 短期记忆 (Redis) - 当前会话上下文
2. 长期记忆 (Qdrant) - 用户画像 + 学习历史
3. 工作记忆 (Dict) - 临时计算结果

对比方案:
- Mem0 AI: 外部服务，开箱即用但依赖网络
- 自建层：完全可控但增加复杂度

本 Demo 选择混合方案:
- 用 Redis 做短期缓存
- 用 Qdrant 做向量检索
- 保留接入 Mem0 API 的能力
"""

import json
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, timezone
import redis
import asyncio
import os
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct


class MemoryManager:
    """记忆管理器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.user_id = None
        
        # 短期记忆 (Redis)
        self.redis_client = redis.Redis.from_url(config.get("redis_url", "redis://127.0.0.1:6379/0"))
        
        # 长期记忆 (Qdrant)
        qdrant_url = config.get("qdrant_url", "http://127.0.0.1:6333")
        self.qdrant_client = QdrantClient(url=qdrant_url)
        
        # 初始化 Collection
        self._init_collections()
    
    def _init_collections(self):
        """初始化 Qdrant 集合"""
        collections = self.qdrant_client.get_collections().collections
        collection_names = [c.name for c in collections]
        
        if "user_profiles" not in collection_names:
            self.qdrant_client.create_collection(
                collection_name="user_profiles",
                vectors_config=VectorParams(size=1536, distance=Distance.COSINE),
            )
        
        if "learning_history" not in collection_names:
            self.qdrant_client.create_collection(
                collection_name="learning_history",
                vectors_config=VectorParams(size=1536, distance=Distance.COSINE),
            )
    
    # ==================== 短期记忆操作 ====================
    
    def save_session_context(self, session_id: str, context: dict):
        """保存会话上下文到 Redis"""
        key = f"session:{session_id}"
        self.redis_client.setex(
            name=key,
            value=json.dumps(context, ensure_ascii=False),
            time=3600  # TTL: 1 小时
        )
    
    def get_session_context(self, session_id: str) -> Optional[dict]:
        """获取会话上下文"""
        key = f"session:{session_id}"
        data = self.redis_client.get(key)
        return json.loads(data.decode()) if data else None
    
    def delete_session(self, session_id: str):
        """删除会话上下文"""
        key = f"session:{session_id}"
        self.redis_client.delete(key)
    
    # ==================== 长期记忆操作 ====================
    
    def set_user_id(self, user_id: str):
        """设置当前用户 ID"""
        self.user_id = user_id
    
    def get_user_profile(self) -> Optional[dict]:
        """获取用户画像"""
        if not self.user_id:
            raise ValueError("User ID not set")
        
        key = f"profile:{self.user_id}"
        data = self.redis_client.get(key)
        return json.loads(data.decode()) if data else None
    
    async def update_user_preferences_async(self, preferences: dict):
        """更新用户偏好（异步版本）"""
        if not self.user_id:
            raise ValueError("User ID not set")
        
        current = self.get_user_profile() or {}
        current.update(preferences)
        
        key = f"profile:{self.user_id}"
        self.redis_client.setex(
            name=key,
            value=json.dumps(current, ensure_ascii=False),
            time=86400 * 30  # 30 天
        )
        
        # 同步写入向量库
        await self._store_user_profile_vector_async(current)
    
    def update_user_preferences(self, preferences: dict):
        """更新用户偏好（同步封装，不推荐在 async 上下文中使用）"""
        if not self.user_id:
            raise ValueError("User ID not set")

        current = self.get_user_profile() or {}
        current.update(preferences)

        key = f"profile:{self.user_id}"
        self.redis_client.setex(
            name=key,
            value=json.dumps(current, ensure_ascii=False),
            time=86400 * 30  # 30 天
        )

        # 同步写入向量库
        try:
            asyncio.get_running_loop()
            # 已有运行中的 event loop，无法再调用 asyncio.run
            print("[WARN] update_user_preferences called in async context. Use update_user_preferences_async instead.")
        except RuntimeError:
            # 没有 running loop，可以安全调用
            asyncio.run(self._store_user_profile_vector_async(current))
    
    async def _store_user_profile_vector_async(self, profile: dict):
        """将用户画像存储到向量数据库（真实向量优先，Mock 仅作紧急降级）"""
        import hashlib

        # Qdrant 要求 ID 是整数或 UUID，使用 MD5 生成唯一整数 ID
        user_id_hash = int(hashlib.md5(str(self.user_id).encode()).hexdigest()[:16], 16)
        payload = {
            "user_id": self.user_id,
            "preferences": profile,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        text = f"User preferences: {json.dumps(profile, ensure_ascii=False)}"
        vector = await self._embed_async(text)

        if vector is not None:
            print(f"✅ User profile vector generated (dim={len(vector)})")
            point = PointStruct(id=user_id_hash, vector=vector, payload=payload)
            self.qdrant_client.upsert(collection_name="user_profiles", points=[point])
            return

        # 紧急降级：真实向量不可用时才使用 Mock（维度需与 collection 一致）
        print("⚠️  Using Mock vector for user profile (emergency fallback)")
        dummy_vector = [0.1] * 1536
        point = PointStruct(id=user_id_hash, vector=dummy_vector, payload=payload)
        self.qdrant_client.upsert(collection_name="user_profiles", points=[point])
    
    async def _embed_async(self, text: str) -> Optional[List[float]]:
        """异步生成文本向量；无 Key 或 API 失败时返回 None（由调用方决定是否降级 Mock）"""
        api_key = os.getenv("ALIYUN_API_KEY") or os.getenv("VOYAGE_API_KEY") or os.getenv("ZHIPU_API_KEY") or ""
        if not api_key:
            return None

        try:
            from src.modules.agent.embedding_service import get_embedding_service
            embedding_service = get_embedding_service(api_key)
            return await embedding_service.generate_embedding(text)
        except Exception as e:
            print(f"[WARN] Vector generation failed: {e}")
            return None

    def _embed_sync(self, text: str) -> Optional[List[float]]:
        """同步生成文本向量；仅在无运行事件循环时可用（异步上下文请用 _embed_async）"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._embed_async(text))

        print("[WARN] Running in async loop; use the async method instead")
        return None

    async def search_similar_users_async(self, target_preferences: dict, top_k: int = 5) -> List[dict]:
        """搜索相似用户（真实向量优先）"""
        text = f"User preferences: {json.dumps(target_preferences, ensure_ascii=False)}"
        query_vector = await self._embed_async(text)
        return self._search_similar_users(query_vector, top_k)

    def search_similar_users(self, target_preferences: dict, top_k: int = 5) -> List[dict]:
        """搜索相似用户（同步封装，异步上下文请用 search_similar_users_async）"""
        text = f"User preferences: {json.dumps(target_preferences, ensure_ascii=False)}"
        query_vector = self._embed_sync(text)
        return self._search_similar_users(query_vector, top_k)

    def _search_similar_users(self, query_vector: Optional[List[float]], top_k: int) -> List[dict]:
        if query_vector is None:
            query_vector = [0.2] * 1536
            print("⚠️  Using Mock vector for user similarity search (emergency fallback)")
        else:
            print(f"✅ User similarity query vector generated (dim={len(query_vector)})")

        results = self.qdrant_client.query_points(
            collection_name="user_profiles",
            query=query_vector,
            limit=top_k,
        )

        return [
            {
                "user_id": point.id,
                "score": point.score,
                "preferences": point.payload.get("preferences", {}),
            }
            for point in results.points
        ]

    async def search_similar_learnings_async(self, query_text: str, top_k: int = 5) -> List[dict]:
        """搜索相似的学习历史（真实向量优先）"""
        query_vector = await self._embed_async(query_text)
        return self._search_similar_learnings(query_vector, top_k)

    def search_similar_learnings(self, query_text: str, top_k: int = 5) -> List[dict]:
        """搜索相似的学习历史（同步封装，异步上下文请用 search_similar_learnings_async）"""
        query_vector = self._embed_sync(query_text)
        return self._search_similar_learnings(query_vector, top_k)

    def _search_similar_learnings(self, query_vector: Optional[List[float]], top_k: int) -> List[dict]:
        if query_vector is None:
            query_vector = [0.4] * 1536
            print("⚠️  Using Mock vector for learning history search (emergency fallback)")
        else:
            print(f"✅ Learning history search vector generated (dim={len(query_vector)})")

        results = self.qdrant_client.query_points(
            collection_name="learning_history",
            query=query_vector,
            limit=top_k,
        )

        return [
            {
                "session_id": point.payload.get("session_id", point.id),
                "score": point.score,
                "event": point.payload,
            }
            for point in results.points
        ]
    
    async def record_learning_event_async(self, event: dict):
        """记录学习事件到历史（真实向量优先，Mock 仅作紧急降级）"""
        if not self.user_id:
            raise ValueError("User ID not set")

        event["user_id"] = self.user_id
        event["timestamp"] = datetime.now(timezone.utc).isoformat()

        import hashlib
        unique_id = f"{self.user_id}:{event['session_id']}"
        event_id_hash = int(hashlib.md5(unique_id.encode()).hexdigest()[:16], 16)

        text = f"{event.get('topic', '')} {event.get('summary', '')}"
        vector = await self._embed_async(text)

        if vector is not None:
            print(f"✅ Learning event vector generated (dim={len(vector)})")
            point = PointStruct(id=event_id_hash, vector=vector, payload=event)
            self.qdrant_client.upsert(collection_name="learning_history", points=[point])
            return

        # 紧急降级：真实向量不可用时才使用 Mock（维度需与 collection 一致）
        print("⚠️  Using Mock vector for learning event (emergency fallback)")
        dummy_vector = [0.3] * 1536
        point = PointStruct(id=event_id_hash, vector=dummy_vector, payload=event)
        self.qdrant_client.upsert(collection_name="learning_history", points=[point])
    
    def get_learning_history(self, limit: int = 10) -> List[dict]:
        """获取用户学习历史"""
        if not self.user_id:
            raise ValueError("User ID not set")
        
        # 从 Redis 获取简化的历史记录
        key = f"history:{self.user_id}"
        history = self.redis_client.lrange(key, 0, limit - 1)
        
        return [json.loads(h.decode()) for h in history]
    
    def add_to_learning_history(self, event: dict):
        """添加到学习历史"""
        if not self.user_id:
            raise ValueError("User ID not set")
        
        key = f"history:{self.user_id}"
        self.redis_client.lpush(key, json.dumps(event, ensure_ascii=False))
        self.redis_client.ltrim(key, 0, 99)  # 只保留最近 100 条
    
    # ==================== 协同过滤推荐 ====================
    
    def recommend_for_new_user(self, news_items: List[dict]) -> List[dict]:
        """为新用户推荐热门内容（Cold Start 解决方案）"""
        # 按热度排序（Mock: 按更新时间降序）
        sorted_items = sorted(news_items, key=lambda x: x.get("created_at", ""), reverse=True)
        return sorted_items[:10]
    
    def recommend_with_collaborative_filtering(self, news_items: List[dict]) -> List[dict]:
        """基于协同过滤的推荐"""
        if not self.user_id:
            return news_items
        
        # 查找相似用户
        similar_users = self.search_similar_users({}, top_k=3)
        
        if not similar_users:
            return self.recommend_for_new_user(news_items)
        
        # 聚合相似用户的偏好
        preference_weights = {}
        for user_data in similar_users:
            prefs = user_data.get("preferences", {})
            weight = user_data["score"]
            
            for tag, confidence in prefs.get("tags", {}).items():
                preference_weights[tag] = preference_weights.get(tag, 0) + confidence * weight
        
        # 根据偏好权重排序
        scored_items = []
        for item in news_items:
            score = sum(
                preference_weights.get(tag, 0) for tag in item.get("tags", [])
            )
            scored_items.append((item, score))
        
        return [item for item, score in sorted(scored_items, key=lambda x: x[1], reverse=True)]


if __name__ == "__main__":
    # 测试示例
    config = {
        "redis_url": "redis://127.0.0.1:6379/0",
        "qdrant_url": "http://127.0.0.1:6333"
    }
    
    mm = MemoryManager(config)
    mm.set_user_id("test-user-123")
    
    print("✅ MemoryManager initialized!")
    print(f"📊 User ID: {mm.user_id}")
    print(f"💾 Redis connected: {mm.redis_client.ping()}")
    print(f"🗄️ Qdrant collections: {[c.name for c in mm.qdrant_client.get_collections().collections]}")
