# Backfill Provenance - 历史卡片溯源字段回填
"""为阶段 3 之前入库的卡片补写 `collection_records` 溯源记录。

为什么需要它：
去重与溯源的真相源在阶段 3 才从 Redis（`crawled:item_ids`）迁到 PostgreSQL，
迁移之前进知识库的老卡片在 DB 里没有记录 —— 结果是「卡片明明在，却查不到来源」，
`/discover/cards/{id}/provenance` 只能回 `known=false`。本脚本把它们补上。

回填是**有损**的，脚本不掩饰这一点：
- `batch_id = NULL`：没有真实批次，就不编造一个批次；
- `summary_generated_at = NULL`：不知道当初什么时候生成的摘要，就不假装知道；
- `collected_at` 取内容发布时间（近似值），而不是「回填那一刻」，
  否则整批历史卡片会被标成同一天采集；
- 统一打上 `backfilled = true`，前端据此如实告知「时间是近似值」。

已有记录一律不覆盖：真实采集信息永远比回填的近似值可信。脚本幂等，可重复执行。

运行:
    cd backend
    .\\ilo\\Scripts\\python.exe backfill_provenance.py --dry-run   # 先看会写多少
    .\\ilo\\Scripts\\python.exe backfill_provenance.py             # 真正写入
"""

import argparse

from loguru import logger

from src.core.config import settings
from src.core.db import is_database_configured, mask_database_url
from src.services.collection_service import backfill_records, get_provenance


def _load_cards():
    """读出知识库全部卡片；知识库不可用返回 None（区别于「一张卡片都没有」）"""
    from src.modules.discovery.tech_knowledge import get_knowledge_base

    try:
        return get_knowledge_base().all_cards()
    except Exception as e:  # noqa: BLE001 - 顶层的失败要如实报告并退出
        logger.error(f"读取知识库失败: {e}")
        return None


def _print_report(stats: dict, dry_run: bool) -> None:
    print("-" * 60)
    print(f"扫描（按 item_id 去重后）: {stats['scanned']}")
    print(f"已有记录（跳过，不覆盖）  : {stats['skipped']}")
    print(f"{'待回填' if dry_run else '已回填'}                  : "
          f"{stats['scanned'] - stats['skipped'] if dry_run else stats['backfilled']}")
    if stats["oversized"]:
        # item_id 超过 64 字符写不进 collection_records，只能如实报告而不是静默丢弃
        print(f"item_id 过长（跳过）      : {stats['oversized']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="历史卡片溯源字段回填")
    parser.add_argument("--dry-run", action="store_true", help="只统计与预览，不写库")
    args = parser.parse_args()

    print("=" * 60)
    print("Backfill Provenance - 历史卡片溯源回填")
    print("=" * 60)

    if not is_database_configured():
        print("✗ 未配置 DATABASE_URL，无法回填。请在 backend/.env 中设置。")
        return 1
    print(f"数据库: {mask_database_url(settings.database_url)}")

    cards = _load_cards()
    if cards is None:
        print("✗ 知识库不可用（Qdrant 未启动？），无法枚举历史卡片。")
        return 1

    print(f"知识库卡片: {len(cards)} 张")
    if args.dry_run:
        print("模式: dry-run（不会写入）")

    try:
        stats = backfill_records(cards, dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001 - 顶层失败要给出非零退出码
        print(f"✗ 回填失败: {e}")
        return 1

    _print_report(stats, args.dry_run)

    if args.dry_run or not cards:
        print("\n完成。")
        return 0

    # 抽样验证：拿第一张卡片回读溯源，确认「有卡片就有来源」这件事真的成立了
    sample_id = str(cards[0].get("id") or "")
    if sample_id:
        record = get_provenance(sample_id)
        known = record is not None
        print("-" * 60)
        print(f"抽样验证 {sample_id}: known={known}, backfilled={record and record['backfilled']}")

    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
