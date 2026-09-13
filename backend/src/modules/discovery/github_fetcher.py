# GitHubFetcher - GitHub 热门仓库抓取器
"""
从 GitHub 官方 Search API 抓取近期热门仓库，并用 LLM 批量提炼中文摘要。

数据流:
[GitHub Search API] -> [GitHubFetcher] -> [LLM 中文摘要] -> [Redis news:feed]
"""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any

import httpx

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"


class GitHubFetcher:
    """GitHub 热门仓库抓取器"""

    def __init__(self):
        self.httpx_client = httpx.AsyncClient(
            timeout=30,
            headers={
                "User-Agent": "ilo-agent-demo",
                "Accept": "application/vnd.github+json",
            },
        )

    async def fetch_trending_repos(self, limit: int = 50, since_days: int = 7) -> List[Dict[str, Any]]:
        """抓取近 N 天创建、按 star 排序的热门仓库（自动翻页，per_page 上限 100）"""
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d")
        items = []
        page = 1
        while len(items) < limit and page <= 10:
            params = {
                "q": f"created:>{since}",
                "sort": "stars",
                "order": "desc",
                "per_page": 100,
                "page": page,
            }
            resp = await self.httpx_client.get(GITHUB_SEARCH_URL, params=params)
            if resp.status_code != 200:
                print(f"[WARN] GitHub Search API 返回 {resp.status_code}，停止翻页")
                break
            data = resp.json()
            batch = data.get("items", [])
            if not batch:
                break
            for repo in batch:
                items.append({
                    "id": f"gh-{repo['id']}",
                    "repo_id": repo["id"],
                    "type": "repo",
                    "title": repo.get("full_name", ""),
                    "summary": repo.get("description") or "",
                    "link": repo.get("html_url", ""),
                    "source": "GitHub Trending",
                    "tags": [repo["language"]] if repo.get("language") else [],
                    "core_concepts": (repo.get("topics") or [])[:3],
                    "stars": repo.get("stargazers_count", 0),
                    "language": repo.get("language"),
                    "created_at": repo.get("created_at"),
                    "updated_at": repo.get("updated_at"),
                })
            page += 1
        return items[:limit]

    async def close(self):
        await self.httpx_client.aclose()


def _extract_json(text: str):
    """从 LLM 输出中稳健提取 JSON：优先找数组，其次找单个对象；
    先严格 json.loads，失败再用 json_repair 自动修复"""
    blob = None
    s = text.find("[")
    e = text.rfind("]")
    if s != -1 and e != -1 and e > s:
        blob = text[s:e + 1]
    else:
        s = text.find("{")
        e = text.rfind("}")
        if s != -1 and e != -1 and e > s:
            blob = text[s:e + 1]
    if blob is None:
        return None
    try:
        parsed = json.loads(blob)
        if isinstance(parsed, list):
            return parsed
        return [parsed]  # 单对象也包成数组，方便统一按索引取
    except Exception:
        pass
    try:
        from json_repair import loads as repair_loads
        repaired = repair_loads(blob)
        if isinstance(repaired, list):
            return repaired
        return [repaired]
    except Exception:
        return None


async def _summarize_batch(batch: List[Dict[str, Any]], kind: str) -> List[Dict[str, Any]]:
    """对单个批次调用 LLM 并解析，成功返回解析后的列表，失败返回空列表"""
    try:
        from src.modules.agent.state_machine import summary_llm
    except Exception:
        summary_llm = None
    if summary_llm is None:
        return []

    from src.modules.discovery.summary_spec import build_summary_prompt

    prompt = build_summary_prompt(batch, kind=kind)
    try:
        result = await asyncio.to_thread(summary_llm.invoke, prompt)
    except Exception as exc:
        print(f"[WARN] LLM 调用失败: {exc}")
        return []
    parsed = _extract_json(result.content)
    if not parsed:
        print(f"[WARN] JSON 解析失败，输出前 120 字符: {(result.content or '')[:120]!r}")
        return []
    return parsed


async def summarize_items(items: List[Dict[str, Any]], kind: str = "repo", batch_size: int = 20) -> List[Dict[str, Any]]:
    """用通义千问批量生成结构化中文技术卡片。

    分批调用，单批失败会自动降级为逐条重试（每条最多重试 1 次），
    避免一整批数据因个别输出格式问题而丢失摘要。
    """
    if not items:
        return items

    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        parsed = await _summarize_batch(batch, kind=kind)
        if parsed and len(parsed) >= len(batch):
            for i, item in enumerate(batch):
                if isinstance(parsed[i], dict):
                    _apply_summary(item, parsed[i])
            continue
        # 整批失败或返回不完整：先应用能用的，再对缺失项逐条重试 1 次
        applied = set()
        if parsed:
            for i, item in enumerate(batch):
                if i < len(parsed) and isinstance(parsed[i], dict):
                    _apply_summary(item, parsed[i])
                    applied.add(i)
        for i, it in enumerate(batch):
            if i in applied:
                continue
            one = await _summarize_batch([it], kind=kind)
            if one and isinstance(one[0], dict):
                _apply_summary(it, one[0])

    return items


def _apply_summary(item: Dict[str, Any], parsed: Dict[str, Any]):
    """把 LLM 结构化结果映射到卡片字段（同时兼容旧字段）"""
    from src.modules.discovery.summary_spec import CATEGORIES

    category = parsed.get("category")
    if category not in CATEGORIES:
        category = "other"
    one_liner = (parsed.get("one_liner") or "").strip()
    problem = (parsed.get("problem") or "").strip()
    intro = (parsed.get("summary") or "").strip()
    tech_stack = parsed.get("tech_stack") or []
    highlights = parsed.get("highlights") or []
    use_cases = parsed.get("use_cases") or []

    item["category"] = category
    item["one_liner"] = one_liner
    item["problem"] = problem
    item["tech_stack"] = tech_stack
    item["highlights"] = highlights
    item["use_cases"] = use_cases

    # summary：优先用 LLM 生成的完整中文简介（40~90 字），缺失时兼容拼接 one_liner + problem
    if intro:
        # 兜底：模型偶尔超长时安全截断（按“句号 > 分号/逗号 > 词边界”逐级回退，避免断词断句）
        if len(intro) > 90:
            cut = intro.rfind("。", 40, 91)
            if cut == -1:
                for sep in ("；", "，", ",", "、"):
                    cut = intro.rfind(sep, 60, 91)
                    if cut != -1:
                        cut += 1  # 保留分隔符后截断位置
                        break
            if cut != -1:
                intro = intro[:cut].rstrip("，,；;、") + "。"
            else:
                # 极少数整段无标点：硬截后去掉可能残缺的尾字
                hard = intro[:88].rstrip("，,；;、 \u201c\u201d\u2018\u2019\"'").rstrip("的了是在与和及为就都而并或但很又")
                intro = hard if hard else intro[:88]
        item["summary"] = intro
    elif one_liner:
        item["summary"] = one_liner + (f"，{problem}" if problem else "")

    # tags：分类 + 语言 + 技术栈（供前端 #标签）
    tags = [category]
    if item.get("language"):
        tags.append(item["language"])
    for t in tech_stack:
        if t not in tags:
            tags.append(t)
    item["tags"] = tags[:6]

    # core_concepts：技术栈（供对话上下文）
    if tech_stack:
        item["core_concepts"] = tech_stack[:5]


if __name__ == "__main__":
    async def _test():
        fetcher = GitHubFetcher()
        print("抓取 GitHub 热门仓库...")
        items = await fetcher.fetch_trending_repos(limit=5)
        print(f"抓到 {len(items)} 条")
        items = await summarize_items(items)
        for i, it in enumerate(items, 1):
            print(f"{i}. {it['title']} ⭐{it.get('stars')} | {it['summary'][:50]}")
        await fetcher.close()

    asyncio.run(_test())
