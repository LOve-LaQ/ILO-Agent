# SimplifiedNewsDiscovery - 简化的技术资讯发现引擎
"""
功能:
- 使用 httpx 直接请求（避免 feedparser 兼容性 issue）
- GitHub Trending 数据抓取
- Mock 技术资讯生成
- 标签分类和去重

注意：为了避开 Python 3.13 的 cgi 模块问题，我们直接请求 API
"""

import httpx
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import hashlib


class SimplifiedNewsDiscoveryEngine:
    """简化的资讯发现引擎"""
    
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.httpx_client = httpx.AsyncClient(timeout=30)
        
        # Mock 技术资讯数据（替代 RSS）
        self.mock_news_template = [
            {
                "title": "Python 异步编程新进展",
                "summary": "最新版本的 Python 带来了更强大的异步特性，包括 asyncio 的改进和新的协程优化...",
                "keywords": ["python", "async", "await", "coroutine"]
            },
            {
                "title": "Rust 1.80 发布：性能进一步提升",
                "summary": "最新的 Rust 版本在编译速度和内存安全性方面都有显著改进...",
                "keywords": ["rust", "performance", "memory-safety"]
            },
            {
                "title": "React 19 Server Components 正式可用",
                "summary": "React 的最新版本引入了 Server Components 的稳定版，大幅改善应用性能...",
                "keywords": ["react", "javascript", "frontend", "server-components"]
            },
            {
                "title": "Docker Desktop 4.30 发布：容器化新体验",
                "summary": "新的 Docker 版本带来了更快的启动速度和更好的 Kubernetes 集成...",
                "keywords": ["docker", "kubernetes", "devops", "containers"]
            },
            {
                "title": "LangChain 0.2 发布：LLM 应用开发框架更新",
                "summary": "LangChain 最新版本支持更多的 LLM 提供商和改进的消息处理机制...",
                "keywords": ["langchain", "llm", "ai", "nlp"]
            },
            {
                "title": "FastAPI 0.110: 异步性能优化",
                "summary": "FastAPI 最新带来了显著的响应时间优化和更好的错误处理...",
                "keywords": ["fastapi", "python", "api", "backend"]
            }
        ]
    
    async def fetch_all_feeds(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取所有资讯（模拟 + 真实混合）"""
        items = []
        
        # 从 GitHub Trending API 获取（如果能访问）
        try:
            trending = await self._fetch_github_trending()
            items.extend(trending)
            print(f"[INFO] Fetched {len(trending)} items from GitHub Trending")
        except Exception as e:
            print(f"[WARN] Failed to fetch GitHub Trending: {e}")
            print("[INFO] Using mock data instead")
        
        # 添加 Mock 数据
        mock_items = self._generate_mock_items()
        items.extend(mock_items)
        
        # 去重并排序
        return self._deduplicate_and_sort(items, limit)
    
    async def _fetch_github_trending(self) -> List[Dict[str, Any]]:
        """从 GitHub Trending 获取热门项目"""
        items = []
        
        try:
            # 尝试直接请求 GitHub API
            response = await self.httpx_client.get(
                "https://github.com/trending",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            
            if response.status_code == 200:
                # 简单解析 HTML（实际应该用 BeautifulSoup）
                html = response.text
                # TODO: 实现真正的解析逻辑
                
        except Exception as e:
            print(f"[ERROR] GitHub API failed: {e}")
        
        return items
    
    def _generate_mock_items(self) -> List[Dict[str, Any]]:
        """生成 Mock 技术资讯"""
        items = []
        now = datetime.now(timezone.utc)
        
        for i, template in enumerate(self.mock_news_template):
            item_id = f"mock-news-{i+1:03d}"
            
            items.append({
                "id": item_id,
                "title": template["title"],
                "summary": template["summary"],
                "link": f"https://example.com/news/{item_id}",
                "published": now.isoformat(),
                "source": "Mock Feed",
                "tags": [],  # 后续填充
                "core_concepts": [],  # 后续填充
                "created_at": now.isoformat(),
                "updated_at": now.isoformat()
            })
        
        return items
    
    def _deduplicate_and_sort(self, items: List[Dict], limit: int = 50) -> List[Dict]:
        """去重并按时间排序"""
        seen = {}
        
        for item in items:
            item_id = item['id']
            if item_id not in seen:
                seen[item_id] = item
            else:
                if item['updated_at'] > seen[item_id]['updated_at']:
                    seen[item_id] = item
        
        sorted_items = sorted(
            list(seen.values()),
            key=lambda x: x['updated_at'],
            reverse=True
        )
        
        return sorted_items[:limit]
    
    def enrich_with_tags(self, items: List[Dict]) -> List[Dict]:
        """为资讯添加标签（启发式规则）"""
        keyword_mapping = {
            'python': ['python', 'django', 'flask', 'fastapi', 'pandas', 'numpy'],
            'javascript': ['javascript', 'react', 'vue', 'angular', 'nodejs'],
            'rust': ['rust', 'cargo', 'macros', 'ownership'],
            'ai_ml': ['ai', 'machine learning', 'llm', 'transformer'],
            'backend': ['api', 'database', 'microservice', 'docker', 'kubernetes'],
            'frontend': ['css', 'html', 'responsive', 'ux', 'ui'],
            'devops': ['docker', 'kubernetes', 'ci/cd', 'deployment']
        }
        
        for item in items:
            text = f"{item['title']} {item['summary']}".lower()
            tags = set()
            
            for tag, keywords in keyword_mapping.items():
                if any(kw in text for kw in keywords):
                    tags.add(tag)
            
            if not tags:
                tags.add('general')
            
            item['tags'] = list(tags)
            
            # 提取核心概念
            concepts = self._extract_concepts(item)
            item['core_concepts'] = concepts
        
        return items
    
    def _extract_concepts(self, item: Dict) -> List[str]:
        """提取核心技术概念"""
        text = item['title'].lower()
        concepts = []
        
        if 'async' in text and ('await' in text or 'coroutine' in text):
            concepts.append('asyncio')
        
        if 'pydantic' in text:
            concepts.extend(['validation', 'data-models'])
        
        if 'vector' in text or 'embedding' in text:
            concepts.append('embeddings')
        
        if not concepts:
            # 根据标签推断概念
            if 'python' in item.get('tags', []):
                concepts.append('python-programming')
            elif 'javascript' in item.get('tags', []):
                concepts.append('web-development')
            else:
                concepts.append('technology')
        
        return concepts[:3]
    
    async def close(self):
        """关闭 HTTP 客户端"""
        await self.httpx_client.aclose()


if __name__ == "__main__":
    import asyncio
    
    async def test_crawler():
        engine = SimplifiedNewsDiscoveryEngine()
        
        print("Fetching news...")
        items = await engine.fetch_all_feeds(limit=30)
        
        print(f"\nOK Retrieved {len(items)} unique items")
        
        # 增强标签
        enriched = engine.enrich_with_tags(items)
        
        print("\nSample items:")
        for i, item in enumerate(enriched[:5], 1):
            print(f"\n{i}. {item['title']}")
            print(f"   Tags: {item['tags']}")
            print(f"   Source: {item['source']}")
        
        await engine.close()
    
    try:
        asyncio.run(test_crawler())
    except KeyboardInterrupt:
        print("\nInterrupted")
