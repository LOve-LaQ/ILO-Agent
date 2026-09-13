# NewsDiscoveryEngine - 技术资讯发现引擎
"""
功能:
- RSS 源订阅（GitHub Trending, Hacker News, TechCrunch 等）
- 内容摘要生成（使用 LLM）
- 标签自动分类
- 去重和热度计算
- 存入向量数据库

核心架构:
[RSS Sources] -> [Extractor] -> [Embedding] -> [Qdrant]
"""

import feedparser
import httpx
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import hashlib
import re
from bs4 import BeautifulSoup


class NewsDiscoveryEngine:
    """技术资讯发现引擎"""
    
    # 常用技术 RSS 源
    RSS_SOURCES = {
        "github_trending": "https://github.com/trending.atom",
        "hacker_news": "https://news.ycombinator.com/rss",
        "techcrunch": "https://techcrunch.com/feed/",
        "reddit_programming": "https://www.reddit.com/r/programming/.rss",
        "rust_weekly": "https://www.rustweekly.com/rss.xml",
        "python_weekly": "https://www.pythonweekly.com/rss.xml"
    }
    
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.httpx_client = httpx.AsyncClient(timeout=30)
        
    async def fetch_all_feeds(self, limit: int = 50) -> List[Dict[str, Any]]:
        """抓取所有 RSS 源"""
        all_items = []
        
        for source_name, url in self.RSS_SOURCES.items():
            try:
                feed = feedparser.parse(url)
                items = self._parse_feed(source_name, feed)
                all_items.extend(items)
                print(f"[INFO] Fetched {len(items)} items from {source_name}")
            except Exception as e:
                print(f"[ERROR] Failed to fetch {source_name}: {e}")
        
        # 按时间排序，去重
        return self._deduplicate_and_sort(all_items, limit)
    
    def _parse_feed(self, source_name: str, feed) -> List[Dict[str, Any]]:
        """解析单个 RSS feed"""
        items = []
        
        for entry in feed.entries[:20]:  # 每个源最多 20 条
            item_id = self._generate_item_id(entry)
            
            # 提取正文内容
            summary = self._extract_summary(entry)
            
            items.append({
                "id": item_id,
                "title": entry.title,
                "summary": summary,
                "link": entry.link if hasattr(entry, 'link') else "",
                "published": getattr(entry, 'published', datetime.now(timezone.utc).isoformat()),
                "source": source_name,
                "tags": [],  # 后续用 LLM 填充
                "core_concepts": [],  # 后续用 LLM 填充
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
        
        return items
    
    def _extract_summary(self, entry) -> str:
        """从条目中提取摘要内容"""
        summary_parts = []
        
        # 优先尝试 content
        if hasattr(entry, 'content'):
            for content in entry.content:
                if hasattr(content, 'value'):
                    summary_parts.append(self._clean_html(content.value))
        
        # 其次尝试 summary
        if hasattr(entry, 'summary'):
            summary_parts.append(self._clean_html(entry.summary))
        
        # 如果没有内容，返回标题
        if not summary_parts:
            return entry.title
        
        return " ".join(summary_parts)[:500]  # 限制长度
    
    def _clean_html(self, html_text: str) -> str:
        """清理 HTML 标签"""
        soup = BeautifulSoup(html_text, 'lxml')
        return soup.get_text(separator=' ', strip=True)
    
    def _generate_item_id(self, entry) -> str:
        """生成唯一 ID"""
        guid = getattr(entry, 'guid', '').value if hasattr(entry, 'guid') else entry.link
        text_hash = hashlib.md5(guid.encode()).hexdigest()[:12]
        return f"news-{text_hash}"
    
    def _deduplicate_and_sort(self, items: List[Dict], limit: int = 50) -> List[Dict]:
        """去重并按时间排序"""
        seen = {}
        
        for item in items:
            item_id = item['id']
            if item_id not in seen:
                seen[item_id] = item
            else:
                # 保留更新的
                if item['updated_at'] > seen[item_id]['updated_at']:
                    seen[item_id] = item
        
        # 按时间排序
        sorted_items = sorted(
            list(seen.values()),
            key=lambda x: x['updated_at'],
            reverse=True
        )
        
        return sorted_items[:limit]
    
    async def enrich_with_llm(self, items: List[Dict], llm_client=None) -> List[Dict]:
        """
        使用 LLM 增强元数据（标签、核心概念）
        
        TODO: 接入真实的 LLM API
        当前使用启发式规则替代
        """
        for item in items:
            item['tags'] = self._extract_tags(item)
            item['core_concepts'] = self._extract_core_concepts(item)
        
        return items
    
    def _extract_tags(self, item: Dict) -> List[str]:
        """基于关键词提取标签"""
        text = f"{item['title']} {item['summary']}".lower()
        tags = set()
        
        # 技术关键词映射
        keyword_mapping = {
            'python': ['python', 'django', 'flask', 'fastapi', 'pandas', 'numpy'],
            'javascript': ['javascript', 'react', 'vue', 'angular', 'nodejs'],
            'rust': ['rust', 'cargo', 'macros', 'ownership'],
            'ai_ml': ['ai', 'machine learning', 'llm', 'transformer', 'neural network'],
            'backend': ['api', 'database', 'microservice', 'docker', 'kubernetes'],
            'frontend': ['css', 'html', 'responsive', 'ux', 'ui'],
            'system': ['linux', 'kernel', 'performance', 'optimization']
        }
        
        for tag, keywords in keyword_mapping.items():
            if any(keyword in text for keyword in keywords):
                tags.add(tag)
        
        # 至少有一个标签
        if not tags:
            tags.add('general')
        
        return list(tags)
    
    def _extract_core_concepts(self, item: Dict) -> List[str]:
        """提取核心技术概念（简化版）"""
        text = item['title'].lower()
        
        concepts = []
        
        # 简单匹配
        if 'async' in text and ('await' in text or 'coroutine' in text):
            concepts.append('asyncio')
        
        if 'pydantic' in text:
            concepts.extend(['validation', 'data-models'])
        
        if 'memory' in text:
            concepts.append('memory-management')
        
        if 'vector' in text or 'embedding' in text:
            concepts.append('embeddings')
        
        if not concepts:
            concepts.append('general')
        
        return concepts[:3]  # 最多 3 个概念
    
    async def close(self):
        """关闭 HTTP 客户端"""
        await self.httpx_client.aclose()


if __name__ == "__main__":
    import asyncio
    
    async def test_crawler():
        engine = NewsDiscoveryEngine()
        
        print("📡 Fetching news feeds...")
        items = await engine.fetch_all_feeds(limit=30)
        
        print(f"\n✅ Retrieved {len(items)} unique items")
        
        # 增强元数据
        enriched = await engine.enrich_with_llm(items)
        
        print("\n📊 First 5 items:")
        for i, item in enumerate(enriched[:5], 1):
            print(f"\n{i}. {item['title']}")
            print(f"   Tags: {item['tags']}")
            print(f"   Source: {item['source']}")
        
        await engine.close()
    
    try:
        asyncio.run(test_crawler())
    except KeyboardInterrupt:
        print("\n️  Interrupted")
