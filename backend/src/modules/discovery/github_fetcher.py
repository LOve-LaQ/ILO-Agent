# GitHubFetcher - GitHub 热门仓库抓取器
"""
从 GitHub 官方 Search API 抓取近期热门仓库，并用 LLM 批量提炼中文摘要。

数据流:
[GitHub Search API] -> [GitHubFetcher] -> [LLM 中文摘要] -> [Redis news:feed]
"""

import asyncio
import base64
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from src.core.config import settings

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
GITHUB_README_URL = "https://api.github.com/repos/{full_name}/readme"
GITHUB_RAW_README_URL = "https://raw.githubusercontent.com/{full_name}/HEAD/{path}"

# README 快照的字符上限：部分项目的 README 上万行，既拖慢渲染也撑大快照。
# 截断后由前端给「查看完整原文」外链 —— 完整版永远能在 GitHub 上看到。
MAX_README_CHARS = 60000

# raw CDN 兜底时的候选文件名（按常见程度排序）
README_FILENAMES = ("README.md", "readme.md", "README.rst", "README.txt", "README.MD")


def _github_headers() -> Dict[str, str]:
    """构造 GitHub 请求头。

    配置了 GITHUB_TOKEN 时带上 Authorization：匿名调用只有 60 次/小时，
    预取 README 会把这点额度迅速耗尽。该头只在此处产生，绝不写进日志或异常信息。
    """
    headers = {
        "User-Agent": "ilo-agent-demo",
        "Accept": "application/vnd.github+json",
    }
    token = (settings.github_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def repo_full_name(url: Optional[str]) -> Optional[str]:
    """从 GitHub 仓库链接解析出 owner/repo，用于拼 README 接口路径"""
    if not url:
        return None
    parsed = urlparse(url)
    if (parsed.netloc or "").lower() not in {"github.com", "www.github.com"}:
        return None
    parts = [segment for segment in (parsed.path or "").split("/") if segment]
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1]}"


class GitHubFetcher:
    """GitHub 热门仓库抓取器"""

    def __init__(self):
        self.httpx_client = httpx.AsyncClient(timeout=30, headers=_github_headers())

    async def fetch_trending_repos(self, limit: int = 50, since_days: int = 7) -> List[Dict[str, Any]]:
        """抓取近 N 天创建、按 star 排序的热门仓库（自动翻页，per_page 上限 100）"""
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d")
        # 溯源时间统一取本次采集时刻（整批一次抓取，不逐条取 now）
        collected_at = datetime.now(timezone.utc).isoformat()
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
                description = repo.get("description") or ""
                items.append({
                    "id": f"gh-{repo['id']}",
                    "repo_id": repo["id"],
                    "type": "repo",
                    "title": repo.get("full_name", ""),
                    "summary": description,
                    "link": repo.get("html_url", ""),
                    "source": "GitHub Trending",
                    "tags": [repo["language"]] if repo.get("language") else [],
                    "core_concepts": (repo.get("topics") or [])[:3],
                    "stars": repo.get("stargazers_count", 0),
                    "language": repo.get("language"),
                    "created_at": repo.get("created_at"),
                    "updated_at": repo.get("updated_at"),
                    # 溯源字段：摘要生成前必须先留下原文，否则 LLM 改写后就再也回不去了
                    "source_platform": "github",
                    "source_url": repo.get("html_url", ""),
                    "raw_description": description,
                    "collected_at": collected_at,
                })
            page += 1
        return items[:limit]

    async def close(self):
        await self.httpx_client.aclose()


def _truncate_readme(text: str) -> tuple:
    """按字符上限截断 README，返回 (文本, 是否被截断)"""
    if len(text) <= MAX_README_CHARS:
        return text, False
    return text[:MAX_README_CHARS], True


async def _readme_via_api(full_name: str, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    """官方 API：能拿到权威的 path 与 blob sha（溯源强度最高）"""
    resp = await client.get(
        GITHUB_README_URL.format(full_name=full_name), headers=_github_headers()
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    encoded = data.get("content") or ""
    if not encoded:
        # 超过 1MB 的文件 API 不返回内联内容，交给 raw CDN 兜底
        return None
    content = base64.b64decode(encoded).decode("utf-8", errors="replace")
    return {
        "content": content,
        "meta": {
            "path": data.get("path"),
            "sha": data.get("sha"),
            "size": data.get("size"),
            "source": "github-api",
        },
    }


async def _readme_via_raw(full_name: str, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    """raw CDN 兜底：不占 API 配额，但拿不到 sha（只知文件名）"""
    for filename in README_FILENAMES:
        url = GITHUB_RAW_README_URL.format(full_name=full_name, path=filename)
        try:
            resp = await client.get(url)
        except Exception:  # noqa: BLE001 - 换下一个候选名继续试
            continue
        if resp.status_code != 200:
            continue
        content = resp.text
        if not content.strip():
            continue
        return {
            "content": content,
            "meta": {
                "path": filename,
                "sha": None,
                "size": len(resp.content),
                "source": "raw-cdn",
            },
        }
    return None


async def fetch_readme(full_name: str, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    """拉取仓库 README 快照，返回 {content, meta}；失败返回 None。

    主路径走官方 API（带 token 时额度 5000/h），API 不可用（限流 / 5xx / 网络异常）
    时降级到 raw CDN —— 「阅读原文」不该因为 token 失效而整体不可用。

    只吞异常不抛：「先读原文」是锦上添花，绝不能连累卡片本身入库。
    """
    for strategy in (_readme_via_api, _readme_via_raw):
        try:
            result = await strategy(full_name, client)
        except Exception as exc:  # noqa: BLE001 - 降级到下一个策略
            print(f"[WARN] README 抓取失败({strategy.__name__}): {type(exc).__name__}")
            continue
        if not result:
            continue
        content, truncated = _truncate_readme(result["content"])
        result["content"] = content
        result["meta"]["truncated"] = truncated
        result["meta"]["fetched_at"] = datetime.now(timezone.utc).isoformat()
        return result
    return None


async def fetch_readme_standalone(full_name: str) -> Optional[Dict[str, Any]]:
    """自建客户端的 README 抓取：按需补抓路径不需要持有 GitHubFetcher 实例"""
    async with httpx.AsyncClient(timeout=30, headers=_github_headers()) as client:
        return await fetch_readme(full_name, client)


async def attach_readmes(items: List[Dict[str, Any]], concurrency: int = 4) -> List[Dict[str, Any]]:
    """为仓库卡补 README 原文快照。

    调用点在去重之后（collect_items 的 enrich 钩子），因此只为真正入库的新卡付费，
    已采集过的卡片不会重复抓。

    并发用信号量压住：GitHub 对突发请求有 secondary rate limit，一次并发几十个
    请求会被判定为滥用并封禁一段时间。
    """
    targets = [
        item
        for item in items
        if item.get("type", "repo") == "repo"
        and repo_full_name(item.get("source_url") or item.get("link"))
    ]
    if not targets:
        return items

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async with httpx.AsyncClient(timeout=30, headers=_github_headers()) as client:

        async def _enrich(item: Dict[str, Any]) -> None:
            full_name = repo_full_name(item.get("source_url") or item.get("link"))
            if not full_name:
                return
            async with semaphore:
                result = await fetch_readme(full_name, client)
            if not result:
                return
            # 私有字段：Qdrant payload 会显式排除（见 tech_knowledge），只进 PostgreSQL
            item["raw_content"] = result["content"]
            item["content_meta"] = result["meta"]

        await asyncio.gather(*(_enrich(item) for item in targets), return_exceptions=True)

    return items


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
    """把 LLM 结构化结果映射到卡片字段（同时兼容旧字段）

    中文化闸门：LLM 偶尔会照抄英文原文（尤其是输入本身就是英文描述时）。一旦放过，
    卡片介绍就直接变成英文，而且因为已经写进知识库，只有下一轮采集再次遇到同一个
    仓库才有机会覆盖 —— 绝大多数情况就是永久英文。所以这里先校验中文，不合格的
    一律丢弃，并**摘掉 item 上残留的英文原描述**，让它保持「未完成」状态被标
    failed 等待重试（collect_items 会据此拒绝入库）。
    """
    from src.modules.discovery.summary_spec import CATEGORIES, is_chinese_text

    one_liner = (parsed.get("one_liner") or "").strip()
    problem = (parsed.get("problem") or "").strip()
    intro = (parsed.get("summary") or "").strip()

    # 中文闸门：卡片主文案与一句话定位都必须含足量汉字
    if not is_chinese_text(intro):
        if intro:
            print(f"[WARN] 摘要非中文，丢弃: {intro[:60]!r}")
        intro = ""
    if not is_chinese_text(one_liner):
        one_liner = ""

    if not intro and not one_liner:
        # 一个可用的中文字段都没有 → 本次摘要视为失败：摘掉英文原描述并清掉 category，
        # 让 collect_items 判为「未完成」（不入库、也拿不到去重标记，下次会重试）。
        # 清之前先把原描述落到 raw_description，保证溯源字段不因这次失败而丢内容。
        if not item.get("raw_description"):
            item["raw_description"] = item.get("summary")
        item.pop("category", None)
        item.pop("summary", None)
        print(f"[WARN] 摘要未产出中文内容，将重试: {item.get('title')}")
        return

    category = parsed.get("category")
    if category not in CATEGORIES:
        category = "other"
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
