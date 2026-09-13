# News Scheduler - 智能资讯抓取调度器
"""
功能:
- 定时抓取最新资讯（可配置间隔）
- 自动去重和分类
- 向量化存储到 Qdrant/Redis
- 支持手动触发

调度策略:
1. 初始抓取：启动时立即抓取一批
2. 定期刷新：每 X 小时自动抓取一次
3. 增量更新：只抓取新内容或更新旧内容

技术栈:
- APScheduler: 轻量级定时任务库
- asyncio: 异步并发抓取
- 缓存机制：避免重复抓取同一 RSS
"""

import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any
import os
from pathlib import Path

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False
    
from loguru import logger
from src.modules.discovery.simplified_engine import NewsDiscoveryEngine


class NewsScheduler:
    """新闻资讯定时抓取服务"""
    
    def __init__(self, config: Dict[str, Any] = None):
        """
        Args:
            config: 调度配置
                - interval_hours: 定时抓取间隔（默认：6 小时）
                - rss_urls: RSS 订阅源列表（可选）
                - auto_sync: 是否自动同步（默认：True）
        """
        self.config = config or {
            "interval_hours": 6,  # 每 6 小时抓取一次
            "rss_urls": [
                "https://feeds.feedburner.com/pythoninstitute/rss",
                "https://www.fastapi.ai/posts.xml",
                "https://pyfound.blogspot.com/feeds/posts/default",
                "https://rsshub.app/ithome/tag/Python",
                "https://rsshub.app/toutiao/computer_science"
            ],
            "auto_sync": True
        }
        
        self.scheduler = None
        self.news_engine = NewsDiscoveryEngine()
        self.last_sync_time = None
        self.total_crawled = 0
        
        logger.info(f"📰 NewsScheduler initialized (APScheduler: {APSCHEDULER_AVAILABLE})")
    
    async def initialize(self):
        """初始化调度器"""
        if not APSCHEDULER_AVAILABLE:
            logger.warning("⚠️  APScheduler not available, using fallback mode")
            return
        
        try:
            self.scheduler = AsyncIOScheduler()
            
            # 添加定时任务：每 X 小时抓取一次
            self.scheduler.add_job(
                func=self.sync_news,
                trigger="cron",
                hour=datetime.now().hour + 1,  # 从下一小时开始
                minute=0,
                second=0,
                max_instances=1,
                misfire_grace_time=3600
            )
            
            # 启动调度器
            self.scheduler.start()
            
            logger.info(f"✅ News scheduler started")
            logger.info(f"   Next sync: Hourly at :{datetime.now():%H:%M}")
            logger.info(f"   Interval: Every {self.config['interval_hours']} hours")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize scheduler: {e}")
            logger.info("ℹ️  Running in manual mode only")
    
    async def sync_news(self):
        """同步最新资讯（供定时任务调用）"""
        logger.info("🔄 Starting scheduled news sync...")
        
        try:
            start_time = datetime.now()
            
            # 1. 获取最新资讯
            new_news = await self.news_engine.fetch_and_store_news(
                limit=20,
                use_cache=False  # 强制刷新
            )
            
            # 2. 统计结果
            now = datetime.now()
            elapsed = (now - start_time).total_seconds()
            
            logger.info(f"✅ Sync completed in {elapsed:.1f}s")
            logger.info(f"   New items: {len(new_news)}")
            self.total_crawled += len(new_news)
            logger.info(f"   Total crawled: {self.total_crawled}")
            
            self.last_sync_time = now.isoformat()
            
            # 3. 如果启用了向量化存储
            if len(new_news) > 0:
                await self._vectorize_news(new_news)
            
        except Exception as e:
            logger.error(f"❌ Sync failed: {e}", exc_info=True)
    
    async def _vectorize_news(self, news_items: List[Dict]):
        """将新闻转换为向量（如果 MemoryManager 可用）"""
        if not APSCHEDULER_AVAILABLE:
            return
            
        try:
            from src.modules.agent.memory_manager import MemoryManager
            
            # 尝试连接 MemoryManager
            config = {
                "redis_url": os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                "qdrant_url": os.getenv("QDRANT_URL", "http://localhost:6333")
            }
            
            mm = MemoryManager(config)
            mm.set_user_id("system-sync")
            
            for news in news_items:
                event = {
                    "type": "news_discovered",
                    "title": news.get("title"),
                    "summary": news.get("summary"),
                    "tags": news.get("tags", []),
                    "url": news.get("link"),
                    "timestamp": news.get("created_at")
                }
                mm.record_learning_event(event)
            
            logger.info(f"🔗 Vectorized {len(news_items)} news items")
            
        except Exception as e:
            logger.debug(f"⚠️  Could not vectorize news (might be expected): {e}")
    
    def stop(self):
        """停止调度器"""
        if self.scheduler:
            self.scheduler.shutdown()
            logger.info("🛑 News scheduler stopped")
    
    def manual_sync(self):
        """手动触发同步（用于测试或即时更新）"""
        if self.scheduler:
            # 立即执行一次同步
            asyncio.run(self.sync_news())
        else:
            logger.info("🔧 Manual sync triggered")
            asyncio.run(self.sync_news())


async def main():
    """主函数（独立运行）"""
    
    print("=" * 60)
    print("ILO-Agent News Scheduler")
    print("=" * 60)
    
    # 创建调度器实例
    scheduler = NewsScheduler({
        "interval_hours": 2,  # 调试模式：每 2 小时抓取一次
        "auto_sync": True
    })
    
    # 初始化并启动
    await scheduler.initialize()
    
    print("\n" + "=" * 60)
    print("Press Ctrl+C to stop the scheduler")
    print("=" * 60)
    
    try:
        # 保持运行
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
