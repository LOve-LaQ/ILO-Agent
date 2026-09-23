# Backfill Repos 30d - 近 30 天 GitHub 热门仓库的一次性灌库
"""把「近 30 天创建」的 GitHub 热门仓库抓进知识库，把榜单视野从 7 天放宽到 30 天。

## 为什么是脚本，而不是改按钮的时间窗

放宽时间窗会让**每一次**手动抓取都变贵变慢（现有 50 条档位实测 213~341s，翻到 10 页
更久），而「补一批存量」是一次性动作。所以走脚本，路由与前端按钮的策略一行不动。

## 策略与按钮完全同源（刻意如此）

本脚本**不自己写一套采集逻辑**，而是复用 `collect_items`，因此下面这些全部同源，
不存在「脚本灌进来的卡片和按钮抓的卡片规则不一样」：

- 去重真相源 = PostgreSQL `collection_records.item_id`（Redis 只是镜像）
- 批次溯源 = `collection_batches`，只是 trigger 记为 `script`，事后能分清是谁触发的
- 中文化闸门 = 摘要未中文化**不入库**，记 failed，留给下轮重试
- README 富化 = 只对真正入库的新卡抓原文快照
- 入库 = Qdrant 按 `md5(item_id)` 覆盖写，向量由卡片字段拼成

唯一差异是 (a) 时间窗 30 天、(b) 翻页数可调。

## 幂等

已采过的仓库会被去重跳过，可重复执行 —— 重复跑只会补真正新增的那些。

## 成本

花钱的只有「新卡摘要」与「README 抓取」，两者都只对新卡发生（去重排在摘要之前）。
所以**先跑 `--dry-run` 看数**，再决定要不要跑真的。

## 运行

    cd backend
    .\\ilo\\Scripts\\python.exe backfill_repos_30d.py --dry-run     # 只抓取+去重，不花一分钱
    .\\ilo\\Scripts\\python.exe backfill_repos_30d.py               # 真正入库
    .\\ilo\\Scripts\\python.exe backfill_repos_30d.py --pages 3     # 只翻 3 页，成本可控
    .\\ilo\\Scripts\\python.exe backfill_repos_30d.py --no-readme   # 跳过 README 富化
"""

import argparse
import asyncio
import math
import time
from typing import Any, Dict, List

from loguru import logger

# GitHub Search 的结果窗口上限就是 1000 条（10 页 × 100），翻更多页也拿不到东西
PAGE_SIZE = 100
MAX_PAGES = 10

# 与 summarize_items 的 batch_size 保持一致，仅用于成本预估
SUMMARY_BATCH_SIZE = 20


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取近 N 天的 GitHub 热门仓库并入库（采集策略与 /discover/refresh 同源）"
    )
    parser.add_argument(
        "--since-days", type=int, default=30, help="只看近多少天创建的仓库（默认 30）"
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=10,
        help=f"翻页数，每页 {PAGE_SIZE} 条，上限 {MAX_PAGES}（默认 10）",
    )
    parser.add_argument(
        "--no-readme", action="store_true", help="跳过 README 原文快照，省 GitHub 请求"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略去重，对抓到的全部条目重新摘要并覆盖写（很贵，慎用）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只抓取+去重并预估成本，不调用 LLM、不写库"
    )
    return parser.parse_args()


async def _fetch(fetcher, limit: int, since_days: int) -> List[Dict[str, Any]]:
    """抓取并打点耗时。

    翻页期间没有别的输出，一次 10 页要静默一阵，容易让人以为卡死 —— 所以把
    「抓了多少、花了多久」明确打出来。
    """
    started = time.monotonic()
    items = await fetcher.fetch_trending_repos(limit=limit, since_days=since_days)
    logger.info(
        f"抓取完成：{len(items)} 条（近 {since_days} 天，耗时 {time.monotonic() - started:.1f}s）"
    )
    return items


def _log_star_distribution(items: List[Dict[str, Any]]) -> None:
    """打印 star 分布。

    「翻 10 页」听起来只是个数字，但 GitHub 搜索是按 star 降序的，页数越靠后 star
    越低 —— 第 10 页很可能是几十 star 的小仓库，对「技术情报」是噪声。翻页数应当是
    看着分布定的，而不是拍一个「10 页」。
    """
    stars = sorted(int(i.get("stars") or 0) for i in items)
    if not stars:
        return
    n = len(stars)
    low = sum(1 for s in stars if s < 50)
    logger.info(
        f"star 分布：最低 {stars[0]} / 中位 {stars[n // 2]} / 最高 {stars[-1]}；"
        f"其中 <50 star 的有 {low} 条（占 {low / n * 100:.0f}%）"
    )


def _report_new(items: List[Dict[str, Any]], force: bool) -> List[Dict[str, Any]]:
    """打印「抓到 / 已采过 / 真正新增」并给出成本预估，返回真正新增的条目。

    这里的去重与 `collect_items` 内部的判定同源（都走 `get_collected_ids`），
    因此这份预估就是实际会发生的工作量，不是拍脑袋。
    """
    from src.services.collection_service import get_collected_ids

    if force:
        logger.warning("--force：跳过去重，全部条目都会重新摘要并覆盖写")
        new_items = items
    else:
        already = get_collected_ids([str(i.get("id")) for i in items])
        new_items = [i for i in items if str(i.get("id")) not in already]

    skipped = len(items) - len(new_items)
    batches = math.ceil(len(new_items) / SUMMARY_BATCH_SIZE) if new_items else 0
    logger.info(
        f"抓到 {len(items)} 条 | 已采过 {skipped} 条 | 新增 {len(new_items)} 条"
    )
    logger.info(
        f"成本预估：摘要约 {batches} 次 LLM 调用；README 抓取最多 {len(new_items)} 次"
    )
    _log_star_distribution(new_items)
    return new_items


async def _run(args: argparse.Namespace) -> int:
    from src.modules.discovery.github_fetcher import GitHubFetcher, attach_readmes
    from src.modules.discovery.tech_knowledge import get_knowledge_base
    from src.services.collection_service import BATCH_TRIGGER_SCRIPT, collect_items

    pages = max(1, min(args.pages, MAX_PAGES))
    if pages != args.pages:
        logger.warning(f"--pages {args.pages} 超出 1~{MAX_PAGES}，收敛为 {pages}")
    limit = pages * PAGE_SIZE

    logger.info(
        f"目标：近 {args.since_days} 天 · 最多 {pages} 页 / {limit} 条 · "
        f"README 富化={'关' if args.no_readme else '开'} · dry_run={args.dry_run}"
    )

    fetcher = GitHubFetcher()
    try:
        # 只抓一次：collect_items 的 fetch 回调复用这份结果，避免把 GitHub 搜索翻两遍
        items = await _fetch(fetcher, limit=limit, since_days=args.since_days)
        if not items:
            logger.error("一条都没抓到 —— GitHub 搜索可能被限流或网络不可达，本次不写库")
            return 1

        new_items = _report_new(items, force=args.force)
        if args.dry_run:
            logger.info("--dry-run：到此为止，未调用 LLM、未写库")
            return 0
        if not new_items:
            logger.info("没有新仓库需要入库，结束（知识库未被修改）")
            return 0

        try:
            kb = await asyncio.to_thread(get_knowledge_base)
        except Exception as e:  # noqa: BLE001 - 顶层的失败要如实报告并退出
            # 刻意不做路由那条 Redis 降级：脚本灌库时「写了 Redis 却没进知识库」
            # 会让这批卡片被永久跳过，比直接失败更糟
            logger.error(f"知识库不可用，拒绝继续: {e}")
            return 1

        async def _replay() -> List[Dict[str, Any]]:
            return items

        result = await collect_items(
            kind="repo",
            fetch=_replay,
            store=kb.upsert,
            trigger=BATCH_TRIGGER_SCRIPT,
            params={
                "source": "backfill_repos_30d",
                "since_days": args.since_days,
                "pages": pages,
                "limit": limit,
                "readme": not args.no_readme,
                "force": args.force,
            },
            force=args.force,
            # 富化只在去重与摘要之后跑，所以只为真正入库的新卡付 GitHub 请求
            enrich=None if args.no_readme else attach_readmes,
        )

        logger.info(
            f"批次 {result.batch_id} 终态={result.status} | 新增 {result.new_count} · "
            f"跳过 {result.skipped_count} · 失败 {result.failed_count}"
        )
        if result.status == "ok":
            logger.info("✅ 全部入库成功")
        elif result.status == "partial":
            logger.warning(
                f"⚠️ {result.failed_count} 条未入库 —— 多数是摘要没过中文化闸门，"
                "已记 failed，下轮采集会重试"
            )
        elif result.status == "error":
            logger.error(
                f"❌ {result.failed_count} 条全部未入库，请查日志与 collection_batches.error"
            )
            return 1

        # 总数以知识库为准，而不是拿 new_count 去加 —— 覆盖写会让两者不相等
        total = await asyncio.to_thread(kb.count, "repo")
        logger.info(f"知识库仓库总数：{total}")
        return 0
    finally:
        await fetcher.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run(_parse_args())))
