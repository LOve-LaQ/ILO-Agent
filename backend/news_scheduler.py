# News Scheduler - 资讯采集定时调度
"""
功能:
- 按固定间隔自动采集热门仓库与多平台技术文章
- 支持手动触发一次采集
- 采集结果与手动「抓取最新」完全一致

为什么要复用 services/collection_service:
定时任务和手动刷新如果各写一套采集逻辑，去重真相源、批次记录就会分叉，
「这张卡片是谁、什么时候抓的」在两处答案不一致。统一编排后，定时任务留下的
是 `collection_batches.trigger = scheduled` 的批次，与手动触发同一套溯源表。

运行:
    cd backend && .\\ilo\\Scripts\\python.exe news_scheduler.py
"""

import asyncio
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False

from loguru import logger

from src.services.collection_service import (
    BATCH_TRIGGER_SCHEDULED,
    CollectResult,
    collect_items,
)


class NewsScheduler:
    """资讯采集定时调度器（定时与手动共用 collection_service）"""

    def __init__(self, config: Dict[str, Any] = None):
        """
        Args:
            config:
                - interval_hours: 定时采集间隔（默认 6 小时）
                - repo_limit: 每次抓取的仓库数上限（默认 50）
                - per_platform: 每个平台抓取的文章数（默认 10）
                - auto_sync: 启动后是否立即采集一次（默认 True）
        """
        config = config or {}
        self.interval_hours = int(config.get("interval_hours", 6))
        self.repo_limit = int(config.get("repo_limit", 50))
        self.per_platform = int(config.get("per_platform", 10))
        self.auto_sync = bool(config.get("auto_sync", True))

        self.scheduler = None
        self.last_sync_time: Optional[str] = None
        self.total_collected = 0

        logger.info(f"📰 NewsScheduler initialized (APScheduler: {APSCHEDULER_AVAILABLE})")

    async def initialize(self) -> None:
        """启动定时调度（APScheduler 缺失时只保留手动模式）"""
        if not APSCHEDULER_AVAILABLE:
            logger.warning("⚠️  APScheduler 不可用，仅支持手动采集")
            return

        try:
            self.scheduler = AsyncIOScheduler()
            # 用 interval 而不是 cron(hour=now.hour+1)：
            # - cron 方案在 23 点会算出 hour=24，直接 ValueError 起不来；
            # - 而且那样 interval_hours 配置根本不会生效（永远一天跑一次）。
            self.scheduler.add_job(
                func=self.sync_news,
                trigger="interval",
                hours=self.interval_hours,
                max_instances=1,
                misfire_grace_time=3600,
            )
            self.scheduler.start()
            logger.info(f"✅ News scheduler started（每 {self.interval_hours} 小时采集一次）")
        except Exception as e:
            logger.error(f"❌ Failed to initialize scheduler: {e}")
            logger.info("ℹ️  Running in manual mode only")

    # ==================== 采集 ====================

    async def _collect_repos(self) -> CollectResult:
        from src.modules.discovery.github_fetcher import GitHubFetcher
        from src.modules.discovery.tech_knowledge import get_knowledge_base

        fetcher = GitHubFetcher()
        try:
            kb = get_knowledge_base()
            return await collect_items(
                kind="repo",
                fetch=lambda: fetcher.fetch_trending_repos(limit=self.repo_limit),
                store=kb.upsert,
                trigger=BATCH_TRIGGER_SCHEDULED,
                params={"limit": self.repo_limit},
            )
        finally:
            await fetcher.close()

    async def _collect_articles(self) -> CollectResult:
        from src.modules.discovery.article_fetcher import ArticleFetcher
        from src.modules.discovery.tech_knowledge import get_knowledge_base

        fetcher = ArticleFetcher()
        try:
            kb = get_knowledge_base()
            return await collect_items(
                kind="article",
                fetch=lambda: fetcher.fetch_articles(per_platform=self.per_platform),
                store=kb.upsert,
                trigger=BATCH_TRIGGER_SCHEDULED,
                params={"per_platform": self.per_platform},
            )
        finally:
            await fetcher.close()

    async def sync_news(self) -> List[CollectResult]:
        """采集一轮（定时任务与手动触发共用）"""
        logger.info("🔄 Starting scheduled collection...")
        collectors: tuple[tuple[str, Callable[[], Any]], ...] = (
            ("repo", self._collect_repos),
            ("article", self._collect_articles),
        )

        results: List[CollectResult] = []
        for label, factory in collectors:
            try:
                results.append(await factory())
            except Exception as e:  # noqa: BLE001 - 单类采集失败不影响另一类
                logger.error(f"❌ {label} 采集失败: {e}")

        new_total = sum(r.new_count for r in results)
        self.total_collected += new_total
        self.last_sync_time = datetime.now().isoformat()
        logger.info(
            f"✅ 采集完成：本轮新增 {new_total} 条，累计 {self.total_collected} 条，"
            f"批次 {[r.batch_id for r in results]}"
        )
        return results

    async def manual_sync(self) -> List[CollectResult]:
        """手动触发一轮采集。

        刻意做成 async：原来的 `asyncio.run()` 在已有事件循环里调用会直接抛
        「asyncio.run() cannot be called from a running event loop」。
        """
        return await self.sync_news()

    def stop(self) -> None:
        """停止调度器"""
        if self.scheduler is not None and getattr(self.scheduler, "running", False):
            self.scheduler.shutdown()
            logger.info("🛑 News scheduler stopped")


async def main() -> None:
    """独立运行入口"""
    print("=" * 60)
    print("ILO-Agent News Scheduler")
    print("=" * 60)

    scheduler = NewsScheduler()
    await scheduler.initialize()

    if scheduler.auto_sync:
        await scheduler.sync_news()

    print("\n" + "=" * 60)
    print("Press Ctrl+C to stop the scheduler")
    print("=" * 60)

    try:
        while True:
            await asyncio.sleep(60)
            print(f"✓ Still running... Last sync: {scheduler.last_sync_time}")
    except KeyboardInterrupt:
        print("\n\nStopping scheduler...")
        scheduler.stop()

    print("\nGoodbye! 👋")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback

        traceback.print_exc()
