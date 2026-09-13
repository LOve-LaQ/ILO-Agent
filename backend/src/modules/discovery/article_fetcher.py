# ArticleFetcher - 技术文章抓取器
"""
从全球几大活跃技术社区抓取热门技术文章：
- Hacker News（Y Combinator）
- Lobsters
- dev.to
- Stack Overflow

数据流:
[多平台 API] -> [ArticleFetcher] -> [通义千问中文摘要] -> [Qdrant 知识库 type=article]
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any

import httpx

HN_URL = "https://hn.algolia.com/api/v1"
LOBSTERS_URL = "https://lobste.rs/hottest.json"
DEVTO_URL = "https://dev.to/api/articles"
SO_URL = "https://api.stackexchange.com/2.3/questions"

# time_range 到各平台参数的映射
DEVTO_TOP = {"day": 1, "week": 7, "month": 30}
SO_SORT = {"day": "hot", "week": "week", "month": "month"}


class ArticleFetcher:
    """多平台技术文章抓取器（单个平台失败不影响其他）"""

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=20,
            headers={"User-Agent": "ilo-agent-demo/1.0"},
        )

    async def fetch_articles(self, per_platform: int = 10, time_range: str = "day") -> List[Dict[str, Any]]:
        """并发抓取各平台热门文章，time_range: day/week/month"""
        tasks = [
            self._hn(per_platform, time_range),
            self._lobsters(per_platform),
            self._devto(per_platform, time_range),
            self._stackoverflow(per_platform, time_range),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        items = []
        for r in results:
            if isinstance(r, list):
                items.extend(r)
            else:
                print(f"[WARN] 某平台抓取失败: {r}")
        return items

    async def _hn(self, n: int, time_range: str) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        if time_range == "day":
            url = f"{HN_URL}/search"
            params = {"tags": "front_page", "hitsPerPage": n}
        else:
            days = 7 if time_range == "week" else 30
            ts = int((now - timedelta(days=days)).timestamp())
            url = f"{HN_URL}/search_by_date"
            params = {"query": "", "tags": "story", "hitsPerPage": n, "numericFilters": f"created_at_i>{ts}"}
        resp = await self.client.get(url, params=params)
        resp.raise_for_status()
        items = []
        for h in resp.json().get("hits", []):
            title = h.get("title") or h.get("story_title") or ""
            if not title:
                continue
            items.append({
                "id": f"hn-{h['objectID']}",
                "type": "article",
                "title": title,
                "summary": (h.get("story_text") or "").strip()[:500],
                "link": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                "source": "Hacker News",
                "score": h.get("points") or 0,
                "comments": h.get("num_comments") or 0,
                "published_at": h.get("created_at"),
                "tags": [],
                "core_concepts": [],
            })
        return items

    async def _lobsters(self, n: int) -> List[Dict[str, Any]]:
        resp = await self.client.get(LOBSTERS_URL)
        resp.raise_for_status()
        items = []
        for a in resp.json()[:n]:
            items.append({
                "id": f"lob-{a.get('short_id')}",
                "type": "article",
                "title": a.get("title", ""),
                "summary": a.get("description") or "",
                "link": a.get("url", ""),
                "source": "Lobsters",
                "score": a.get("score") or 0,
                "comments": a.get("comment_count") or 0,
                "published_at": a.get("created_at"),
                "tags": (a.get("tags") or [])[:4],
                "core_concepts": [],
            })
        return items

    async def _devto(self, n: int, time_range: str) -> List[Dict[str, Any]]:
        top = DEVTO_TOP.get(time_range, 7)
        resp = await self.client.get(DEVTO_URL, params={"top": top, "per_page": n})
        resp.raise_for_status()
        items = []
        for a in resp.json():
            items.append({
                "id": f"dev-{a['id']}",
                "type": "article",
                "title": a.get("title", ""),
                "summary": a.get("description") or "",
                "link": a.get("url", ""),
                "source": "dev.to",
                "score": a.get("positive_reactions_count") or 0,
                "comments": a.get("comments_count") or 0,
                "published_at": a.get("published_at"),
                "tags": (a.get("tag_list") or [])[:4],
                "core_concepts": [],
            })
        return items

    async def _stackoverflow(self, n: int, time_range: str) -> List[Dict[str, Any]]:
        sort = SO_SORT.get(time_range, "hot")
        resp = await self.client.get(SO_URL, params={
            "order": "desc", "sort": sort, "site": "stackoverflow", "pagesize": n,
        })
        resp.raise_for_status()
        items = []
        for q in resp.json().get("items", []):
            items.append({
                "id": f"so-{q['question_id']}",
                "type": "article",
                "title": q.get("title", ""),
                "summary": "",
                "link": q.get("link", ""),
                "source": "Stack Overflow",
                "score": q.get("score") or 0,
                "comments": q.get("answer_count") or 0,
                "published_at": datetime.fromtimestamp(q.get("creation_date", 0), tz=timezone.utc).isoformat(),
                "tags": (q.get("tags") or [])[:4],
                "core_concepts": [],
            })
        return items

    async def close(self):
        await self.client.aclose()
