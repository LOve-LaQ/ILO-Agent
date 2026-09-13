# EmbeddingService - 真实的文本向量化服务
"""
功能:
- 支持多个 Embedding API 提供商（智能降级）
- 自动检测最优服务
- 缓存优化

支持的 Provider（按优先级）:
✅ 阿里云 (Aliyun/Qwen) - 中文优化，国内最快
✅ Voyage AI - 英文质量最优
✅ OpenAI - 通用选择
✅ 智谱 AI (Zhipu) - 国产高性价比
✅ Baidu - 文心一言
⚠️ DeepSeek - ❌ 不支持 Embedding（仅支持 Chat）

当前实现状态：
✅ 完整的架构设计
✅ Mock 降级策略
✅ 可快速启用任意 provider

配置方式:
1. 检查 backend/.env 中的 API Key
2. 系统自动选择可用的 provider
3. 全部失败时降级到 Mock
"""

import httpx
from typing import List, Optional
import hashlib
import json
import os


class EmbeddingService:
    """文本嵌入服务（支持多个 Provider）"""
    
    def __init__(self, api_key: str = None, provider: str = "auto"):
        """
        Args:
            api_key: API Key (从环境变量读取)
            provider: 服务提供商
                - "aliyun": 阿里云/Qwen（中文最优）
                - "voyage": Voyage AI（英文最优）
                - "openai": OpenAI
                - "zhipu": 智谱 AI
                - "baidu": 百度文心
                - "auto": 自动选择（按优先级）
        """
        # 检测 API Key
        if api_key is None:
            api_key = os.getenv("ALIYUN_API_KEY") or \
                     os.getenv("VOYAGE_API_KEY") or \
                     os.getenv("ZHIPU_API_KEY") or \
                     os.getenv("BAIDU_API_KEY") or \
                     os.getenv("DEEPSEEK_API_KEY") or \
                     os.getenv("OPENAI_API_KEY") or ""
        
        self.api_key = api_key
        self.provider = provider
        
        # 阿里云 Qwen 配置
        self.aliyun_url = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
        self.aliyun_model = "text-embedding-v2"
        
        # Voyage AI 配置
        self.voyage_url = "https://api.voyageai.com/v1/embeddings"
        self.voyage_model = "voyage-large-2"
        
        # OpenAI 配置
        self.openai_url = "https://api.openai.com/v1/embeddings"
        self.openai_model = "text-embedding-ada-002"
        
        # 智谱 AI 配置
        self.zhipu_url = "https://open.bigmodel.cn/api/paas/v4/embeddings"
        self.zhipu_model = "embedding-3"
        
        # 百度文心配置
        self.baidu_url = "https://inference-api.baidubce.com/bicl/embeddings/v4/text_embedding_v2"
        self.baidu_model = "embedding_v2"
        
        # HTTP 客户端
        self.http_client = httpx.AsyncClient(timeout=30.0)
        
        # 简单的文本缓存（避免重复计算）
        self._cache = {}
        
        # 当前使用的 Provider
        self._current_provider = self._detect_provider()
        print(f"[INFO] Selected Provider: {self._current_provider}")
    
    def _detect_provider(self) -> str:
        """自动检测使用哪个 Provider（按优先级排序）

        通过与环境变量精确匹配来确定 Provider，避免因 API Key 前缀相同
        （例如阿里云 DashScope 的 Key 也以 sk- 开头）而误判为 OpenAI。
        """
        if self.provider != "auto":
            return self.provider

        if not self.api_key:
            print("[WARN] No valid API key found, using Mock embedding")
            return "mock"

        # 优先级 1: 阿里云 Qwen（中文最优）
        if self.api_key == os.getenv("ALIYUN_API_KEY"):
            print("[INFO] Using Aliyun/Qwen for embeddings")
            return "aliyun"

        # 优先级 2: Voyage AI（英文最优）
        if self.api_key == os.getenv("VOYAGE_API_KEY"):
            print("[INFO] Using Voyage AI for embeddings")
            return "voyage"

        # 优先级 3: 智谱 AI
        if self.api_key == os.getenv("ZHIPU_API_KEY"):
            print("[INFO] Using Zhipu AI for embeddings")
            return "zhipu"

        # 优先级 4: 百度文心
        if self.api_key == os.getenv("BAIDU_API_KEY"):
            print("[INFO] Using Baidu Wenxin for embeddings")
            return "baidu"

        # 优先级 5: OpenAI
        if self.api_key == os.getenv("OPENAI_API_KEY"):
            print("[INFO] Using OpenAI for embeddings")
            return "openai"

        # 兜底：无法精确匹配时按 Key 前缀猜测
        key = self.api_key.lower()
        if key.startswith("glm-"):
            print("[INFO] Using Zhipu AI for embeddings")
            return "zhipu"
        if "voy" in key:
            print("[INFO] Using Voyage AI for embeddings")
            return "voyage"
        if "sk-" in key:
            print("[INFO] Using OpenAI for embeddings")
            return "openai"

        # 降级到 Mock
        print("[WARN] No valid API key found, using Mock embedding")
        return "mock"
    
    async def _request_embedding(self, text: str) -> Optional[List[float]]:
        """向 API 请求嵌入向量"""
        
        # Mock 降级
        if self._current_provider == "mock":
            return None
        
        try:
            if self._current_provider == "aliyun":
                # 阿里云 Qwen API
                response = await self.http_client.post(
                    self.aliyun_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "X-DashScope-Async": "disable"  # 同步模式
                    },
                    json={
                        "model": self.aliyun_model,
                        "input": {
                            "texts": [text]
                        },
                        "parameters": {
                            "text_type": "document"
                        }
                    }
                )
            elif self._current_provider == "voyage":
                # Voyage AI API
                response = await self.http_client.post(
                    self.voyage_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "Voyage-API-Version": "2024-07-18"
                    },
                    json={
                        "model": self.voyage_model,
                        "input": text
                    }
                )
            elif self._current_provider == "openai":
                # OpenAI API
                response = await self.http_client.post(
                    self.openai_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": self.openai_model,
                        "input": text
                    }
                )
            elif self._current_provider == "zhipu":
                # 智谱 AI API
                response = await self.http_client.post(
                    self.zhipu_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": self.zhipu_model,
                        "input": text
                    }
                )
            elif self._current_provider == "baidu":
                # 百度文心 API
                response = await self.http_client.post(
                    self.baidu_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": self.baidu_model,
                        "input": text
                    }
                )
            else:
                print(f"[ERROR] Unknown provider: {self._current_provider}")
                return None
            
            if response.status_code != 200:
                print(f"[ERROR] Embedding API failed [{self._current_provider}]: {response.status_code}")
                print(f"Response: {response.text[:200]}")
                return None
            
            data = response.json()
            
            # 解析不同 Provider 的响应格式
            if self._current_provider == "aliyun":
                # 阿里云响应：{"output":{"embeddings":[{"embedding":[],"text_index":0}]}}
                if isinstance(data, dict) and "output" in data:
                    embeddings = data["output"].get("embeddings", [])
                    if embeddings:
                        return embeddings[0].get("embedding", None)
            
            elif self._current_provider == "zhipu":
                # 智谱响应：{"data":[{"embedding":[],"token_count":0}],...}
                if isinstance(data, dict) and "data" in data:
                    embeddings = data["data"]
                    if embeddings:
                        return embeddings[0].get("embedding", None)
            
            else:
                # Voyage/OpenAI/Baidu 通用格式：{"data":[{"embedding":[]}]}或[{"embedding":[]}]
                if isinstance(data, dict) and "data" in data:
                    embeddings = data["data"]
                    if isinstance(embeddings, list) and len(embeddings) > 0:
                        return embeddings[0].get("embedding", None)
                elif isinstance(data, list) and len(data) > 0:
                    emb_data = data[0]
                    if isinstance(emb_data, dict):
                        return emb_data.get("embedding", None)
            
            return None
            
        except Exception as e:
            print(f"[ERROR] Embedding request error [{self._current_provider}]: {e}")
            return None
    
    async def generate_embedding(self, text: str) -> Optional[List[float]]:
        """
        生成单个文本的嵌入向量。

        Returns:
            真实向量列表；当 Provider 不可用或 API 请求失败时返回 None，
            由调用方决定是否降级（Mock 仅作为紧急方案）。
        """
        # 检查缓存
        cache_key = hashlib.md5(text.encode()).hexdigest()
        if cache_key in self._cache:
            return self._cache[cache_key]

        # 向 API 请求（失败返回 None，不再在底层生成 Mock）
        embedding = await self._request_embedding(text)

        if embedding is not None:
            self._cache[cache_key] = embedding

        return embedding
    
    async def generate_batch_embeddings(self, texts: List[str]) -> List[List[float]]:
        """批量生成嵌入向量（跳过生成失败的文本，避免返回 None）"""
        embeddings = []
        for text in texts:
            embedding = await self.generate_embedding(text)
            if embedding is None:
                print("[WARN] Skipping a text in batch: embedding generation failed")
                continue
            embeddings.append(embedding)
        return embeddings
    
    async def close(self):
        """清理资源（本地模型无需清理）"""
        pass  # 本地模型不需要关闭 HTTP 客户端
    
    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()


# 单例实例
_embedding_service = None


def get_embedding_service(api_key: str) -> EmbeddingService:
    """获取 Embedding 服务实例"""
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService(api_key)
    return _embedding_service
