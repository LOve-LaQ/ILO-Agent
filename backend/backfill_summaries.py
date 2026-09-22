# Backfill Summaries - 历史英文卡片的中文化回填
"""把知识库里「简介没中文化」的存量卡片重新摘要并写回。

为什么会有英文卡片：
`collect_items` 此前在「摘要是否成功」判断**之前**就无条件 `store()`，于是一旦
LLM 摘要整批失败（或返回不完整），卡片就带着 GitHub 的英文原描述进了知识库。
因为卡片介绍直接展示 `summary`，用户看到的就是一张英文卡片；而且只有下一轮采集
再次碰到同一个仓库才会被覆盖 —— 榜单早已换了一批，实际等同于永久英文。

修复已在采集链路落地（摘要未中文化则拒绝入库 + 保留重试），本脚本负责**清存量**：
把已经在库的英文卡片逐张重新摘要，只有产出合格中文才写回；仍然失败的一律不动
（保留原状，等下一轮采集重试），绝不为了「跑完」而写进半成品。

幂等：中文已合格的卡片直接跳过，可重复执行。

运行:
    cd backend
    .\\ilo\\Scripts\\python.exe backfill_summaries.py --dry-run   # 先看有多少张
    .\\ilo\\Scripts\\python.exe backfill_summaries.py             # 真正重摘要并写回
"""

import argparse

from loguru import logger

from src.modules.discovery.summary_spec import is_chinese_text
from src.modules.discovery.tech_knowledge import get_knowledge_base


def _load_cards():
    """读出知识库全部卡片；知识库不可用返回 None（区别于「一张卡片都没有」）"""
    try:
        return get_knowledge_base().all_cards()
    except Exception as e:  # noqa: BLE001 - 顶层的失败要如实报告并退出
        logger.error(f"读取知识库失败: {e}")
        return None


def _needs_chinese_summary(card: dict) -> bool:
    """卡片简介是否还没中文化（空简介同样算不合格）"""
    return not is_chinese_text(card.get("summary") or "")


def _material_item(card: dict) -> dict:
    """把知识库里的卡片还原成摘要链路的输入。

    摘要 prompt 的素材取自 `summary` 字段，而这里的 `summary` 正是要修掉的英文
    原描述，所以显式改用 `raw_description` 作为素材；两者都空时交给模型按标题判断。
    """
    item = dict(card)
    material = (card.get("raw_description") or card.get("summary") or "").strip()
    item["summary"] = material
    return item


async def _resummarize(items: list) -> list:
    """对候选卡片逐张重摘要，返回 (成功写回的 item 列表, 仍失败的 item 列表)"""
    from src.modules.discovery.github_fetcher import summarize_items
    from src.services.collection_service import _summary_applied

    kb = get_knowledge_base()
    fixed, failed = [], []

    for raw_card in items:
        item = _material_item(raw_card)
        item_id = str(item.get("id") or "")
        kind = item.get("type") or "repo"
        # 逐张调用（batch_size=1）：单张失败不会连累其他卡片，也便于逐条如实报告
        try:
            await summarize_items([item], kind=kind, batch_size=1)
        except Exception as e:  # noqa: BLE001 - 单张失败不中断整轮回填
            logger.warning(f"[WARN] 重摘要异常: {item_id} - {e}")
            failed.append(item)
            continue

        if not _summary_applied(item):
            # 模型仍没产出合格中文：保持原状，等采集链路自然重试
            failed.append(item)
            continue

        try:
            await kb.upsert(item)
        except Exception as e:  # noqa: BLE001 - 写回失败等同这张没修
            logger.warning(f"[WARN] 写回知识库失败: {item_id} - {e}")
            failed.append(item)
            continue

        fixed.append(item)
        print(f"  ✓ {item_id} | {item.get('title')}")
        print(f"      {item.get('summary')}")

    return fixed, failed


def _print_report(total: int, candidates: int, fixed: int, failed: int, dry_run: bool) -> None:
    print("-" * 60)
    print(f"知识库卡片总数        : {total}")
    print(f"简介未中文化（待处理）: {candidates}")
    if dry_run:
        print("已修复                : 0（dry-run，不会写入）")
    else:
        print(f"已修复（写回知识库）  : {fixed}")
        print(f"仍失败（保持原状重试）: {failed}")


def main() -> int:
    parser = argparse.ArgumentParser(description="历史英文卡片的中文化回填")
    parser.add_argument("--dry-run", action="store_true", help="只统计与预览，不调模型不写库")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少张（0 = 不限制）")
    args = parser.parse_args()

    print("=" * 60)
    print("Backfill Summaries - 历史英文卡片中文化回填")
    print("=" * 60)

    cards = _load_cards()
    if cards is None:
        print("✗ 知识库不可用（Qdrant 未启动？），无法枚举历史卡片。")
        return 1

    candidates = [c for c in cards if _needs_chinese_summary(c)]
    if args.limit > 0:
        candidates = candidates[: args.limit]

    print(f"知识库卡片: {len(cards)} 张")
    if args.dry_run:
        print("模式: dry-run（不会调模型、不会写入）")

    if args.dry_run or not candidates:
        _print_report(len(cards), len(candidates), 0, 0, dry_run=True)
        for c in candidates[:20]:
            print(f"  - {c.get('id')} | {c.get('title')} | {str(c.get('summary'))[:60]!r}")
        print("\n完成。")
        return 0

    import asyncio

    fixed, failed = asyncio.run(_resummarize(candidates))
    _print_report(len(cards), len(candidates), len(fixed), len(failed), dry_run=False)

    print("\n完成。")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
