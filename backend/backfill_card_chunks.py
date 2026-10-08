# Backfill Card Chunks - README 原文切块 + 向量化入库
"""把 `collection_records.raw_content`（README 原文）切块、向量化，写进 Qdrant
的 `card_chunks` 集合。

这是 RAG 的**离线建索引**环节：做完这一步，「检索」才有东西可查。

## 正文的真相源

向量库里只存 `card_id` + `char_start` / `char_end`，不存正文。所以本脚本必须先
从 PostgreSQL 读原文 —— Qdrant 里的卡片 payload 刻意排除了 `raw_content`
（见 `tech_knowledge.upsert`），README 原文只存在于 `collection_records`。

## 幂等与原子性

- **幂等**：点 ID 是 `hash(card_id, ordinal)`，同一张卡重跑会覆盖同一批点。
  默认跳过已在库里的卡，不重复付向量化费用。
- **原子（每张卡）**：一张卡的**全部块都向量化成功**才写入；只要有一块失败，
  这张卡一个块都不写。因此「在库里」严格等价于「这张卡已完成」，不存在写了一半
  的中间态 —— 否则跳过逻辑会把半成品永久固化，而且从外面看不出来。
- **重切要 `--rechunk`**：改过切分参数或换过向量模型后必须用它，它会先删掉该卡
  的旧块。不删的重切会留下**孤儿块**（旧的第 N+1..M 块指向的 `char_start/char_end`
  在新切分下已经指到别的文字上），检索命中它们会拿到语义对不上的正文，且不报错。

## 运行

    cd backend

    # 1) 先体检：不调模型、不写库，看块数与 token 估算
    .\\ilo\\Scripts\\python.exe backfill_card_chunks.py --dry-run

    # 2) 小样本：确认端到端通、块数一致、向量非退化，并拿到**真实** token 消耗
    .\\ilo\\Scripts\\python.exe backfill_card_chunks.py --limit 20

    # 3) 全量
    .\\ilo\\Scripts\\python.exe backfill_card_chunks.py

    # 单卡排查
    .\\ilo\\Scripts\\python.exe backfill_card_chunks.py --card-id gh-123 --rechunk

## 成本

有 API 费用（embedding）。`--dry-run` 零成本。正式跑之前先跑小样本拿真实 token
数，再线性放大估全量 —— 别用字符数拍脑袋估，中英文的字符/token 比差一倍多。
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv

# 【为什么显式给路径】无参 `load_dotenv()` 从**当前工作目录**往上找 .env，
# 从项目根运行就会找不到，然后所有 Key 静默为空、退化成占位向量 ——
# 最坏的结果是「看起来跑成功了，其实一个真向量都没写」。锚到脚本所在目录，
# 从哪运行都对。
load_dotenv(Path(__file__).resolve().parent / ".env")

from loguru import logger  # noqa: E402 - 必须在 load_dotenv 之后
from sqlalchemy import select  # noqa: E402

from src.core.config import settings  # noqa: E402
from src.core.db import get_session_factory, mask_database_url  # noqa: E402
from src.models.collection import CollectionRecord  # noqa: E402
from src.modules.rag.chunk_spec import (  # noqa: E402
    COLLECTION,
    VECTOR_SIZE,
    build_chunk_payload,
    build_embedding_text,
    estimate_tokens,
)
from src.modules.rag.chunker import chunk_markdown  # noqa: E402
from src.modules.rag.chunk_store import (  # noqa: E402
    chunk_embedding_fingerprint,
    get_chunk_store,
)

# 成本估算用的参考单价（元 / 千 token）。**这是估算，不是账单** ——
# 各家、各档位、各活动期都不一样，默认值按阿里云 DashScope text-embedding-v2
# 的公开牌价填。以真实账单为准，这里只用来在跑之前判断量级。
_DEFAULT_PRICE_PER_1K = 0.0007


def _embedding_api_key() -> str:
    """按 EmbeddingService 的优先级取 Key（与 tech_knowledge / interest_profile 一致）"""
    import os

    return (
        os.getenv("ALIYUN_API_KEY")
        or os.getenv("VOYAGE_API_KEY")
        or os.getenv("ZHIPU_API_KEY")
        or os.getenv("BAIDU_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    )


# ==================== 读候选卡片 ====================


def load_cards(limit: int = 0, card_id: str = "") -> List[Dict[str, Any]]:
    """读出有 README 原文的采集记录。

    `raw_content` 只有 GitHub 抓取链路会写（`github_fetcher.py`），HN / dev.to
    的文章路径没有全文 —— 所以这里筛出来的就是「目前能切块的语料」全集。
    """
    stmt = (
        select(
            CollectionRecord.item_id,
            CollectionRecord.raw_content,
            CollectionRecord.source_url,
            CollectionRecord.card_payload,
        )
        .where(CollectionRecord.raw_content.isnot(None))
        .where(CollectionRecord.raw_content != "")
        .order_by(CollectionRecord.item_id)
    )
    if card_id:
        stmt = stmt.where(CollectionRecord.item_id == card_id)
    if limit > 0:
        stmt = stmt.limit(limit)

    with get_session_factory()() as session:
        rows = session.execute(stmt).all()

    cards: List[Dict[str, Any]] = []
    for item_id, raw_content, source_url, card_payload in rows:
        payload = card_payload or {}
        cards.append(
            {
                "card_id": str(item_id),
                "raw_content": raw_content or "",
                "source_url": source_url or "",
                "title": str(payload.get("title") or ""),
            }
        )
    return cards


# ==================== 切块 ====================


def build_card_chunks(card: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str], int]:
    """切块并组装向量化输入。

    Returns:
        `(items, embedding_texts, truncated_count)`
        items 每项形如 `{"card_id", "ordinal", "payload"}`，向量留到后面填。
    """
    raw = card["raw_content"]
    chunks = chunk_markdown(raw)

    items: List[Dict[str, Any]] = []
    embedding_texts: List[str] = []
    truncated = 0

    for chunk in chunks:
        text, was_truncated = build_embedding_text(chunk, title=card["title"])
        if was_truncated:
            truncated += 1
        embedding_texts.append(text)
        items.append(
            {
                "card_id": card["card_id"],
                "ordinal": int(chunk["ordinal"]),
                "payload": build_chunk_payload(
                    card_id=card["card_id"],
                    chunk=chunk,
                    source_url=card["source_url"],
                    title=card["title"],
                    # 指纹留空，由调用方在真正入库时统一填 —— 干跑时不该去碰
                    # EmbeddingService 单例（那会建 HTTP 客户端）。
                    embedding_fingerprint=None,
                ),
            }
        )
    return items, embedding_texts, truncated


# ==================== 主流程 ====================


async def run(args) -> int:
    started = time.monotonic()

    api_key = _embedding_api_key()
    if not api_key and not args.dry_run:
        print("✗ 没有可用的 embedding API Key（ALIYUN_API_KEY 等均为空），无法向量化。")
        print("  请在 backend/.env 中配置，或先用 --dry-run 只看切块结果。")
        return 1

    fingerprint = chunk_embedding_fingerprint()

    # ---- 读候选 ----
    try:
        cards = load_cards(limit=args.limit, card_id=args.card_id)
    except Exception as e:  # noqa: BLE001 - 顶层的失败要如实报告并退出
        print(f"✗ 读取 collection_records 失败: {e}")
        print("  请确认 PostgreSQL 已启动且 DATABASE_URL 正确。")
        return 1

    print("-" * 64)
    print(f"数据库    : {mask_database_url(settings.database_url)}")
    print(f"Qdrant    : {settings.qdrant_url}")
    print(f"向量指纹  : {fingerprint or '（未取到，不入库）'}")
    print(f"候选卡片  : {len(cards)} 张（有 README 原文）")
    if not cards:
        print("\n没有可处理的卡片。")
        return 0

    # ---- 切块（零成本，先把全部结果算出来，好报准确的量级） ----
    print("\n切块中……")
    prepared: List[Tuple[Dict[str, Any], List[Dict[str, Any]], List[str], int]] = []
    total_chunks = 0
    total_truncated = 0
    est_tokens = 0
    empty_cards: List[str] = []

    for card in cards:
        items, texts, truncated = build_card_chunks(card)
        if not items:
            empty_cards.append(card["card_id"])
            continue
        prepared.append((card, items, texts, truncated))
        total_chunks += len(items)
        total_truncated += truncated
        est_tokens += sum(estimate_tokens(t) for t in texts)

    print(f"  产出块数      : {total_chunks}")
    print(f"  平均每张卡    : {total_chunks / max(1, len(prepared)):.1f} 块")
    print(f"  无块可写      : {len(empty_cards)} 张（原文过短或全是空结构）")
    print(f"  超预算需截断  : {total_truncated} 块")
    print(f"  token 估算    : {est_tokens:,}（约 ¥{est_tokens / 1000 * args.price_per_1k:.4f}）")

    if args.dry_run:
        print("\n[dry-run] 不调模型、不写库。")
        print("  按上面的块数与 token 估算确认量级后，先跑 `--limit 20` 拿真实消耗。")
        for card in empty_cards[:10]:
            print(f"  - 无块: {card}")
        return 0

    # ---- 连向量库 ----
    try:
        store = get_chunk_store()
    except Exception as e:  # noqa: BLE001
        print(f"\n✗ 向量库不可用: {e}")
        return 1

    before = store.count()
    print(f"\n集合 {COLLECTION}: 现有 {before} 个块")

    # ---- 幂等：挑出还没入库的卡 ----
    card_ids = [c["card_id"] for c, _, _, _ in prepared]
    if args.rechunk:
        todo_ids = set(card_ids)
        print(f"模式      : --rechunk（先删旧块，全部重切重算）")
    else:
        try:
            indexed = store.indexed_card_ids(card_ids)
        except Exception as e:  # noqa: BLE001
            print(f"✗ 查询已入库卡片失败: {e}")
            return 1
        todo_ids = {cid for cid in card_ids if cid not in indexed}
        print(f"已入库跳过: {len(card_ids) - len(todo_ids)} 张")
    print(f"本次处理  : {len(todo_ids)} 张")

    todo = [p for p in prepared if p[0]["card_id"] in todo_ids]
    if not todo:
        print("\n没有需要处理的卡片。")
        return 0

    # ---- 向量化 + 写入 ----
    from src.modules.agent.embedding_service import get_embedding_service

    service = get_embedding_service(api_key)
    service.reset_usage()

    written_chunks = 0
    ok_cards = 0
    failed: List[Tuple[str, str]] = []
    trunc_written = 0

    for position, (card, items, texts, truncated) in enumerate(todo, start=1):
        card_id = card["card_id"]
        try:
            vectors = await service.embed_texts(texts, text_type="document")
        except Exception as e:  # noqa: BLE001 - 单卡失败不中断整轮
            failed.append((card_id, f"向量化异常: {e}"))
            print(f"[{position}/{len(todo)}] ✗ {card_id} 向量化异常: {e}")
            continue

        missing = [i for i, v in enumerate(vectors) if v is None]
        if missing:
            # 原子性：只要有一块没算出来，这张卡整张不写。否则库里会出现
            # 「块序残缺」的卡，而跳过逻辑会把它当成已完成，永久固化。
            failed.append((card_id, f"{len(missing)}/{len(items)} 块向量化失败"))
            print(
                f"[{position}/{len(todo)}] ✗ {card_id} "
                f"{len(missing)}/{len(items)} 块向量化失败，整张跳过"
            )
            continue

        if fingerprint is None:
            failed.append((card_id, "取不到 embedding 指纹，拒绝写入"))
            print(f"[{position}/{len(todo)}] ✗ {card_id} 取不到 embedding 指纹")
            continue

        for item, vector in zip(items, vectors):
            item["vector"] = vector
            item["payload"]["embedding_fingerprint"] = fingerprint

        try:
            if args.rechunk:
                store.delete_card(card_id)
            written = store.upsert_chunks(items)
        except Exception as e:  # noqa: BLE001 - 单卡写入失败不中断整轮
            failed.append((card_id, f"写入失败: {e}"))
            print(f"[{position}/{len(todo)}] ✗ {card_id} 写入失败: {e}")
            continue

        written_chunks += written
        trunc_written += truncated
        ok_cards += 1
        title = (card["title"] or "")[:40]
        print(f"[{position}/{len(todo)}] ✓ {card_id} | {title} | {written} 块")

    # ---- 报告 ----
    elapsed = time.monotonic() - started
    tokens = service.total_tokens
    after = store.count()

    print("-" * 64)
    print(f"卡片: 成功 {ok_cards} / 失败 {len(failed)}")
    print(f"块  : 写入 {written_chunks}（集合 {before} → {after}）")
    print(f"截断: {trunc_written} 块（超出向量模型 token 预算，尾部被砍）")
    print(f"耗时: {elapsed:.1f}s")
    print(f"token: {tokens:,}（API 报的 usage，非估算）")
    print(f"成本: 约 ¥{tokens / 1000 * args.price_per_1k:.4f}"
          f"（按 ¥{args.price_per_1k}/千 token 估算，以账单为准）")

    if failed:
        print(f"\n失败清单（前 20 条）:")
        for card_id, reason in failed[:20]:
            print(f"  - {card_id}: {reason}")
        print("  失败是「整张卡没写」，重跑本脚本会自动重试这些卡。")

    print("\n完成。")
    return 0 if not failed else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="README 原文切块 + 向量化入库（card_chunks）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="只切块与估算，不调模型不写库（零成本）")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多处理多少张卡（0 = 全部）；小样本验证用")
    parser.add_argument("--card-id", default="",
                        help="只处理指定的卡（排查单卡用）")
    parser.add_argument("--rechunk", action="store_true",
                        help="忽略幂等、先删旧块再重切重算（改过切分参数或换过模型时用）")
    parser.add_argument("--price-per-1k", type=float, default=_DEFAULT_PRICE_PER_1K,
                        help=f"成本估算单价（元/千 token），默认 {_DEFAULT_PRICE_PER_1K}")
    args = parser.parse_args()

    print("=" * 64)
    print("Backfill Card Chunks - README 原文切块 + 向量化入库")
    print("=" * 64)

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已中断。已写入的卡片保持完成状态，重跑会自动跳过它们。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
