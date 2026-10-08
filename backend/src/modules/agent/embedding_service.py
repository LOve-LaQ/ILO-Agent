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
✅ 多 Provider 降级策略
✅ 可快速启用任意 provider

配置方式:
1. 检查 backend/.env 中的 API Key
2. 系统自动选择可用的 provider
3. 全部失败时降级到占位向量
"""

import httpx
from typing import List, Optional, Sequence
import hashlib
import json
import os
import time

from src.core import circuit_breaker
from src.core.config import settings
from src.core.resilience import (
    RETRY_ON_NETWORK,
    TRANSIENT_HTTP_STATUSES,
    log_downstream_failure,
    with_retry,
)


def _read_timeout_s(client) -> float:
    """尽力读出 HTTP 客户端配置的读超时，读不到就返回 0。

    【为什么要兜一层】这只是给观测日志补一个 timeout_s 字段。客户端可能被替换成
    桩 / mock（没有 timeout 属性），或者 timeout 配成了 None —— 若因此抛异常，
    就会把**原始的下游异常**顶掉，把一次网络故障伪装成一个 AttributeError，
    真正的原因反而丢了。观测字段永远不该反过来炸掉主流程。
    """
    try:
        value = getattr(getattr(client, "timeout", None), "read", None)
        return float(value) if value is not None else 0.0
    except Exception:  # noqa: BLE001 - 观测字段不得影响主流程
        return 0.0


# 批量向量化的单次条数。阿里云 DashScope 的 text-embedding-v2 上限就是 25 条，
# 其余 Provider 按同一档切分（它们上限更大，但统一档位最省心）。
#
# 实测（2026-10-05，千问 text-embedding-v2）：
#   单条热态 ≈ 0.59s，批量 25 条 ≈ 0.72s —— 批量几乎没有额外开销。
#   13,468 个块：串行约 2.2 小时，按 25 条批量约 6.5 分钟，差 20 倍以上。
EMBEDDING_BATCH_SIZE = 25

# text_type 用于**非对称检索**：文档侧传 "document"，查询侧传 "query"。
# 只对阿里云生效（其余 Provider 无此参数，会被忽略）。
TEXT_TYPE_DOCUMENT = "document"
TEXT_TYPE_QUERY = "query"


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
        self.http_client = httpx.AsyncClient(timeout=settings.embedding_timeout)
        
        # 简单的文本缓存（避免重复计算）
        self._cache = {}

        # 累计消耗的 token 数（来自各 Provider 的 usage 字段）。
        # 存在的意义是让批量入库脚本能报出**真实**成本，而不是靠字数估算 ——
        # 中英文的字符/token 比差很多（实测中文约 0.57 token/字符），估不准。
        self.total_tokens = 0

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
            print("[WARN] No valid API key found, using placeholder embedding")
            return "fallback"

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

        # 降级到占位向量
        print("[WARN] No valid API key found, using placeholder embedding")
        return "fallback"
    
    async def _post(self, url: str, *, headers: dict, json: dict) -> httpx.Response:
        """带重试 + 熔断的 POST。

        只重试**网络层**瞬时故障（连接被重置 / 读超时）：embedding 是入库链路的
        关键，一次抖动就落占位向量会在语义空间里留空洞。HTTP 4xx（Key 错、额度
        耗尽）重试一万次结果一样，不重试 —— 仍由调用方按既有分支返回 None。

        【为什么 5xx 不重试、却计入熔断】网络抖动是「不确定的瞬时故障」，重试一次
        值得；5xx 是下游**已经明确宣告**自己不行了，重试只是在替它续命。这类失败
        交给熔断处理：连续几次之后整个下游直接跳过，比逐个文本各等一次超时划算。
        """
        # 熔断打开：连 embedding 服务都连续失败了，本批入库直接走占位/跳过，
        # 不再逐个文本去撞一次（每个都要等满超时）。
        if circuit_breaker.is_open(circuit_breaker.EMBEDDING):
            raise circuit_breaker.CircuitOpenError("embedding circuit open")

        _started = time.monotonic()
        try:
            response = await with_retry(
                lambda: self.http_client.post(url, headers=headers, json=json),
                attempts=2,
                base_delay=0.5,
                retry_on=RETRY_ON_NETWORK,
                label="embedding",
            )
        except Exception as exc:
            circuit_breaker.record_failure(circuit_breaker.EMBEDDING)
            log_downstream_failure(
                downstream="embedding",
                exc=exc,
                elapsed_ms=(time.monotonic() - _started) * 1000,
                timeout_s=_read_timeout_s(self.http_client),
                # 同样用 getattr 兜底：观测字段读不到就留空，不能顶掉原始异常
                scope=getattr(self, "_current_provider", "") or "",
            )
            raise

        # 【计数口径】5xx / 429 计入熔断；4xx 既不算成功也不算失败。
        # 4xx 是「我们的请求有问题」（Key 错、参数错），与下游健康度无关：
        # 算成成功会把失败计数反复清零（零散网络故障永远攒不到阈值），
        # 算成失败则一个配错的 Key 就能把熔断永久顶在 OPEN，反而掩盖真问题。
        if response.status_code in TRANSIENT_HTTP_STATUSES:
            circuit_breaker.record_failure(circuit_breaker.EMBEDDING)
        elif response.status_code < 400:
            circuit_breaker.record_success(circuit_breaker.EMBEDDING)
        return response

    async def _request_embedding(
        self, text: str, text_type: str = TEXT_TYPE_DOCUMENT
    ) -> Optional[List[float]]:
        """向 API 请求嵌入向量"""
        
        # 降级模式
        if self._current_provider == "fallback":
            return None
        
        try:
            if self._current_provider == "aliyun":
                # 阿里云 Qwen API
                response = await self._post(
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
                            "text_type": text_type
                        }
                    }
                )
            elif self._current_provider == "voyage":
                # Voyage AI API
                response = await self._post(
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
                response = await self._post(
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
                response = await self._post(
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
                response = await self._post(
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
    
    async def generate_embedding(
        self, text: str, text_type: str = TEXT_TYPE_DOCUMENT
    ) -> Optional[List[float]]:
        """
        生成单个文本的嵌入向量。

        Args:
            text: 待向量化文本
            text_type: 非对称检索的侧别。文档侧传 "document"，**查询侧要传 "query"**。
                仅对阿里云生效；用错侧别会让检索精度悄悄下降（不报错，只是排序变差）。

        Returns:
            真实向量列表；当 Provider 不可用或 API 请求失败时返回 None，
            由调用方决定是否降级（占位向量仅作为紧急方案）。
        """
        # 检查缓存
        cache_key = hashlib.md5(f"{text_type}:{text}".encode()).hexdigest()
        if cache_key in self._cache:
            return self._cache[cache_key]

        # 向 API 请求（失败返回 None，不再在底层生成占位向量）
        embedding = await self._request_embedding(text, text_type)

        if embedding is not None:
            self._cache[cache_key] = embedding

        return embedding
    
    async def generate_batch_embeddings(self, texts: List[str]) -> List[List[float]]:
        """批量生成嵌入向量（跳过生成失败的文本，避免返回 None）

        ⚠️ 这是早期的逐条串行实现，**已不推荐使用**：它用 for 循环逐个 await，
        13,468 个块要跑 2 小时以上。新代码请用 `embed_texts`。

        更要紧的是它的返回语义有问题：失败的文本会被**跳过**而不是占位，调用方
        因此无法把返回的向量和入参一一对应 —— 一旦中间有一条失败，后面全部错位，
        而且不会报错。保留它只是为了不破坏既有调用方。
        """
        embeddings = []
        for text in texts:
            embedding = await self.generate_embedding(text)
            if embedding is None:
                print("[WARN] Skipping a text in batch: embedding generation failed")
                continue
            embeddings.append(embedding)
        return embeddings

    # ==================== 真批量 ====================

    async def _batch_aliyun(
        self, texts: Sequence[str], text_type: str
    ) -> List[Optional[List[float]]]:
        """阿里云批量向量化

        按响应里的 `text_index` 回填，**不假设返回顺序与入参一致** —— 接口不保证
        顺序，靠位置对齐会在中间某条失败时整体错位，而且错位是静默的：向量还是
        合法的向量，只是配错了文本。
        """
        out: List[Optional[List[float]]] = [None] * len(texts)
        response = await self._post(
            self.aliyun_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-DashScope-Async": "disable",
            },
            json={
                "model": self.aliyun_model,
                "input": {"texts": list(texts)},
                "parameters": {"text_type": text_type},
            },
        )

        if response.status_code != 200:
            print(
                f"[ERROR] Embedding batch failed [{self._current_provider}]: "
                f"{response.status_code} {response.text[:200]}"
            )
            return out

        data = response.json()
        self._record_usage(data)

        embeddings = (data.get("output") or {}).get("embeddings") or []
        for item in embeddings:
            index = item.get("text_index")
            vector = item.get("embedding")
            if isinstance(index, int) and 0 <= index < len(out) and vector:
                out[index] = vector
        return out

    async def _batch_aliyun_resilient(
        self, texts: Sequence[str], text_type: str
    ) -> List[Optional[List[float]]]:
        """批量向量化，但对超长文本做**逐块降级**而不是整批失败

        【为什么必须这样】阿里云批量请求里只要**一条**超限，整批 400 —— 25 条
        一起白跑。而超限是「这一块太长」这种局部问题，不该让同批另外 24 条陪葬。
        真实后果是整个回填脚本卡在同一张卡上反复失败（踩过：一张 60 块的卡
        永远写不进去，而且报错只说「25/60 块失败」，看不出根因）。

        流程：整批发 → 若失败，逐条重发 → 逐条还失败的走 `SHRINK_LADDER`
        按比例截断再发。最坏情况是「极少数块被截断」，而不是「整张卡写不进去」。

        【为什么用固定档位而不是二分】每次验证都要真的发一次请求（付费）。
        二分找精确边界要 log2(长度)≈11 次调用，而档位最多 5 次。超长块在真实
        语料里只占 0.007%，不值得为它做精确边界搜索 —— 砍掉 15% 尾巴换少 6 次
        调用，划算。
        """
        vectors = await self._batch_aliyun(texts, text_type)
        if all(v is not None for v in vectors):
            return vectors

        from src.modules.rag.chunk_spec import SHRINK_LADDER, shrink_by_length

        missing = [i for i, v in enumerate(vectors) if v is None]
        print(
            f"[WARN] 批量 {len(texts)} 条中 {len(missing)} 条失败，转为逐条重发"
            f"（超长会整批 400，这里做局部降级）"
        )
        for index in missing:
            original = texts[index]
            vector = await self.generate_embedding(original, text_type)
            if vector is not None:
                vectors[index] = vector
                continue

            for ratio in SHRINK_LADDER:
                candidate = shrink_by_length(original, target_ratio=ratio)
                if len(candidate) >= len(original):
                    continue
                vector = await self.generate_embedding(candidate, text_type)
                if vector is not None:
                    print(
                        f"[WARN] 第 {index} 条超长，已截断至 {len(candidate)}/"
                        f"{len(original)} 字符（比例 {ratio}）后入库"
                    )
                    vectors[index] = vector
                    break
            else:
                print(f"[WARN] 第 {index} 条即使截到 25% 仍被拒，放弃该块")
        return vectors

    async def _batch_openai_like(self, texts: Sequence[str]) -> List[Optional[List[float]]]:
        """OpenAI / Voyage / 智谱 的批量协议：`input` 直接是字符串数组

        这三家的响应是 `data[i].embedding`，且**带 `index` 字段**，同样按它回填。
        """
        if self._current_provider == "openai":
            url, model = self.openai_url, self.openai_model
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
        elif self._current_provider == "voyage":
            url, model = self.voyage_url, self.voyage_model
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Voyage-API-Version": "2024-07-18",
            }
        else:
            url, model = self.zhipu_url, self.zhipu_model
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

        out: List[Optional[List[float]]] = [None] * len(texts)
        response = await self._post(
            url, headers=headers, json={"model": model, "input": list(texts)}
        )

        if response.status_code != 200:
            print(
                f"[ERROR] Embedding batch failed [{self._current_provider}]: "
                f"{response.status_code} {response.text[:200]}"
            )
            return out

        data = response.json()
        self._record_usage(data)

        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return out
        for position, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            if not isinstance(index, int):
                index = position
            vector = item.get("embedding")
            if 0 <= index < len(out) and vector:
                out[index] = vector
        return out

    def _record_usage(self, data: dict) -> None:
        """累计 token 消耗（各家字段名不完全一致，取不到就跳过）"""
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):
            self.total_tokens += int(usage.get("total_tokens") or 0)

    async def _request_embedding_batch(
        self, texts: Sequence[str], text_type: str
    ) -> List[Optional[List[float]]]:
        """按 Provider 分发批量请求

        百度文心的批量协议**没有实测过**，所以走逐条回退 —— 猜一个没验证过的
        协议，只会把「协议不对」伪装成「向量化失败」，更难排查。
        """
        if not texts:
            return []
        if self._current_provider == "aliyun":
            # 走 resilient 版本：阿里云是「一条超长整批 400」，必须能局部降级
            return await self._batch_aliyun_resilient(texts, text_type)
        if self._current_provider in {"openai", "voyage", "zhipu"}:
            return await self._batch_openai_like(texts)
        return [await self._request_embedding(t, text_type) for t in texts]

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        text_type: str = TEXT_TYPE_DOCUMENT,
        batch_size: int = EMBEDDING_BATCH_SIZE,
    ) -> List[Optional[List[float]]]:
        """批量向量化，**返回与入参等长**的列表，失败位置为 None

        与 `generate_batch_embeddings` 的关键差别是返回语义：失败**不跳过**，
        而是原地留 None。调用方因此可以安全地用 `zip(texts, vectors)` 对齐 ——
        这正是批量入库脚本必需的：向量和块必须一一对应，错位是静默的灾难。

        Args:
            texts: 待向量化文本
            text_type: "document"（入库）或 "query"（检索）。见 `generate_embedding`
            batch_size: 单次请求的条数，默认 25（阿里云上限）

        Returns:
            等长列表；失败位置为 None（含 Provider 不可用、熔断打开、单条解析失败）
        """
        if batch_size <= 0:
            raise ValueError("batch_size 必须为正数")

        results: List[Optional[List[float]]] = [None] * len(texts)
        if not texts or self._current_provider == "fallback":
            return results

        # 缓存命中先填掉，剩下的才发请求
        pending: List[tuple[int, str, str]] = []
        for index, raw in enumerate(texts):
            cache_key = hashlib.md5(f"{text_type}:{raw}".encode()).hexdigest()
            cached = self._cache.get(cache_key)
            if cached is not None:
                results[index] = cached
            else:
                pending.append((index, raw, cache_key))

        for start in range(0, len(pending), batch_size):
            window = pending[start : start + batch_size]
            try:
                vectors = await self._request_embedding_batch(
                    [raw for _, raw, _ in window], text_type
                )
            except Exception as exc:  # noqa: BLE001
                # 网络层失败（_post 已重试过并记了熔断）：这一批整体留 None，
                # 让调用方决定重跑还是跳过，不要因为一批失败炸掉整轮入库。
                print(f"[WARN] Embedding batch raised, marking {len(window)} texts failed: {exc}")
                continue

            for (index, _raw, cache_key), vector in zip(window, vectors):
                results[index] = vector
                if vector is not None:
                    self._cache[cache_key] = vector

        return results

    def reset_usage(self) -> None:
        """清零 token 计数（入库脚本开始前调用，好让统计只覆盖本轮）"""
        self.total_tokens = 0

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
