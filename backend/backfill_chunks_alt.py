# Backfill Chunks Alt - 用**另一种切分策略**建一个对照集合
"""P3 主门禁「按标题切 vs 固定长度」需要两套块索引做对照，本脚本建对照组。

## 为什么必须单独建集合

两套切分策略的块**不能混在同一个集合里**：点 ID 是 `hash(card_id, ordinal)`，
两种切分的第 N 块会互相覆盖，剩下的就是「孤儿块」（见 `chunk_store.delete_card`
的说明）。所以对照组落在 `card_chunks_fixed<N>` 这样的独立集合里。

## 为什么固定长度值得当对照组

`chunker.py` 走的是「优先按标题切、超长再按长度切」的**结构优先**策略。要证明
「按标题切」真的更好，就必须和「完全不看结构、纯按长度切」比。这是评测集存在
的**主要理由** —— 没有它，切分参数的选择只能靠块数这类代理指标（而块数少
不等于检索好）。

## 区间不变式

对照组同样必须满足 `text == 原文[char_start:char_end]` —— 否则评测集里那些
锚定在字符区间上的标签就没法和它对上。本脚本的切法只在**边界**上做取舍
（往左找换行），从不改写正文。

## 运行

    cd backend

    # 1) 干跑：只看块数与 token 估算（零成本）
    .\\ilo\\Scripts\\python.exe backfill_chunks_alt.py --size 1200 --dry-run

    # 2) 小样本
    .\\ilo\\Scripts\\python.exe backfill_chunks_alt.py --size 1200 --limit 20

    # 3) 全量（约 1.7 万块，十几分钟）
    .\\ilo\\Scripts\\python.exe backfill_chunks_alt.py --size 1200

    # 4) 评测对照
    .\\ilo\\Scripts\\python.exe retrieval_runner.py --collection card_chunks_fixed1200 --tag fixed1200
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from loguru import logger  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.models import (  # noqa: E402
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)

from src.core.config import settings  # noqa: E402
from src.core.resilience import qdrant_timeout_seconds  # noqa: E402
from src.modules.rag.chunk_spec import (  # noqa: E402
    VECTOR_SIZE,
    build_chunk_payload,
    build_embedding_text,
    chunk_point_id,
    estimate_tokens,
)

_DEFAULT_PRICE_PER_1K = 0.0007
_UPSERT_BATCH = 128
_SCROLL_PAGE = 500


# ==================== 对照组切分器 ====================


def chunk_fixed(
    text: str, *, size: int = 1200, overlap_ratio: float = 0.15
) -> List[Dict[str, Any]]:
    """纯按长度切（**完全不看 Markdown 结构**）

    Args:
        text: 原文
        size: 单块目标字符数
        overlap_ratio: 相邻块重叠比例

    Returns:
        每项 `{text, char_start, char_end, ordinal, heading_path}`，
        **不变式**：`text == 原文[char_start:char_end]`。

    唯一的「聪明」之处：在窗口尾部往回找一个换行符断开。这只影响 `char_end`
    取值，不动正文 —— 不变式因此仍然成立。
    """
    if size <= 0:
        raise ValueError("size 必须为正数")
    if not 0 <= overlap_ratio < 1:
        raise ValueError("overlap_ratio 必须落在 [0, 1) 内")
    if not text:
        return []

    step = max(1, int(size * (1 - overlap_ratio)))
    overlap = int(size * overlap_ratio)
    n = len(text)
    out: List[Dict[str, Any]] = []
    start = 0
    ordinal = 0

    while start < n:
        end = min(start + size, n)
        if end < n:
            # 只在窗口后半段找换行，避免把块切得过短
            nl = text.rfind("\n", start + step, end)
            if nl > start:
                end = nl + 1

        seg = text[start:end]
        if seg.strip():
            out.append(
                {
                    "text": seg,
                    "char_start": start,
                    "char_end": end,
                    "ordinal": ordinal,
                    "heading_path": [],
                }
            )
            ordinal += 1

        if end >= n:
            break
        start = max(end - overlap, start + step)

    return out


# ==================== 读卡片 ====================


def load_cards(limit: int = 0) -> List[Dict[str, Any]]:
    """复用入库脚本的读取逻辑（同一张卡、同一个 title 口径）"""
    from backfill_card_chunks import load_cards as _load

    return _load(limit=limit)


# ==================== 主流程 ====================


def _ensure_collection(client: QdrantClient, name: str) -> bool:
    names = [c.name for c in client.get_collections().collections]
    if name in names:
        return False
    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )
    return True


def _indexed_card_ids(client: QdrantClient, name: str, card_ids: Sequence[str]) -> set:
    found: set = set()
    ids = [str(i) for i in card_ids if i]
    for start in range(0, len(ids), 256):
        window = ids[start : start + 256]
        offset = None
        while True:
            batch, offset = client.scroll(
                collection_name=name,
                scroll_filter=Filter(
                    must=[FieldCondition(key="card_id", match=MatchAny(any=window))]
                ),
                limit=_SCROLL_PAGE,
                offset=offset,
                with_payload=["card_id"],
                with_vectors=False,
                timeout=qdrant_timeout_seconds(settings.qdrant_timeout),
            )
            if not batch:
                break
            for p in batch:
                cid = (p.payload or {}).get("card_id")
                if cid:
                    found.add(str(cid))
            if offset is None:
                break
    return found


async def run(args) -> int:
    collection = args.collection or f"card_chunks_fixed{args.size}"
    started = time.monotonic()

    cards = load_cards(limit=args.limit)
    print(f"候选卡片: {len(cards)} 张")

    prepared: List[Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]] = []
    total_chunks = 0
    est = 0
    for card in cards:
        chunks = chunk_fixed(
            card["raw_content"], size=args.size, overlap_ratio=args.overlap_ratio
        )
        if not chunks:
            continue
        texts: List[str] = []
        items: List[Dict[str, Any]] = []
        for ch in chunks:
            t, _ = build_embedding_text(ch, title=card["title"])
            texts.append(t)
            items.append(
                {
                    "card_id": card["card_id"],
                    "ordinal": int(ch["ordinal"]),
                    "payload": build_chunk_payload(
                        card_id=card["card_id"],
                        chunk=ch,
                        source_url=card["source_url"],
                        title=card["title"],
                        embedding_fingerprint=None,
                    ),
                }
            )
        prepared.append((card, items, texts))
        total_chunks += len(items)
        est += sum(estimate_tokens(t) for t in texts)

    print(f"集合     : {collection}")
    print(f"块数     : {total_chunks}")
    print(f"token 估算: {est:,}（约 ¥{est / 1000 * args.price_per_1k:.4f}）")

    if args.dry_run:
        print("\n[dry-run] 不调模型、不写库。")
        return 0

    from src.modules.agent.embedding_service import get_embedding_service
    from src.modules.rag.retriever import _embedding_api_key

    api_key = _embedding_api_key()
    if not api_key:
        print("✗ 没有可用的 embedding API Key")
        return 1

    client = QdrantClient(url=settings.qdrant_url, timeout=settings.qdrant_timeout)
    created = _ensure_collection(client, collection)
    print(f"集合{'已创建' if created else '已存在'}")

    if not args.rechunk:
        done = _indexed_card_ids(
            client, collection, [c["card_id"] for c, _, _ in prepared]
        )
        prepared = [p for p in prepared if p[0]["card_id"] not in done]
        print(f"已入库跳过: {len(done)} 张 → 本次处理 {len(prepared)} 张")

    service = get_embedding_service(api_key)
    try:
        service.reset_usage()
    except Exception:  # noqa: BLE001
        pass

    ok = 0
    written = 0
    failed: List[Tuple[str, str]] = []
    for i, (card, items, texts) in enumerate(prepared, start=1):
        cid = card["card_id"]
        try:
            vectors = await service.embed_texts(texts, text_type="document")
        except Exception as e:  # noqa: BLE001 - 单卡失败不中断整轮
            failed.append((cid, f"向量化异常: {e}"))
            print(f"[{i}/{len(prepared)}] ✗ {cid} 向量化异常: {e}")
            continue

        missing = [j for j, v in enumerate(vectors) if v is None]
        if missing:
            failed.append((cid, f"{len(missing)}/{len(items)} 块失败"))
            print(f"[{i}/{len(prepared)}] ✗ {cid} {len(missing)}/{len(items)} 块失败，整张跳过")
            continue

        points = []
        for item, vec in zip(items, vectors):
            item["payload"]["embedding_fingerprint"] = f"fixed{args.size}"
            points.append(
                PointStruct(
                    id=chunk_point_id(cid, item["ordinal"]),
                    vector=list(vec),
                    payload=item["payload"],
                )
            )
        try:
            for s in range(0, len(points), _UPSERT_BATCH):
                client.upsert(
                    collection_name=collection,
                    points=points[s : s + _UPSERT_BATCH],
                    timeout=qdrant_timeout_seconds(settings.qdrant_write_timeout),
                )
        except Exception as e:  # noqa: BLE001
            failed.append((cid, f"写入失败: {e}"))
            print(f"[{i}/{len(prepared)}] ✗ {cid} 写入失败: {e}")
            continue

        written += len(points)
        ok += 1
        if i % 25 == 0 or i == len(prepared):
            print(f"[{i}/{len(prepared)}] ✓ 累计写入 {written} 块")

    elapsed = time.monotonic() - started
    total = client.count(collection_name=collection, exact=True).count
    tokens = int(getattr(service, "total_tokens", 0) or 0)
    print("-" * 64)
    print(f"卡片: 成功 {ok} / 失败 {len(failed)}")
    print(f"块  : 本次写入 {written}（集合现有 {total}）")
    print(f"耗时: {elapsed:.1f}s")
    print(f"token: {tokens:,}（API 上报）/ 估算 {est:,}")
    print(f"成本: 约 ¥{tokens / 1000 * args.price_per_1k:.4f}（按 ¥{args.price_per_1k}/千 token）")
    if failed:
        print("失败清单（前 10）:")
        for cid, why in failed[:10]:
            print(f"  - {cid}: {why}")
    print("\n完成。")
    return 0 if not failed else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description="用固定长度切分建对照集合（P3 主门禁用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--size", type=int, default=1200, help="单块目标字符数")
    p.add_argument("--overlap-ratio", type=float, default=0.15, help="重叠比例")
    p.add_argument("--collection", default="", help="集合名（默认 card_chunks_fixed<size>）")
    p.add_argument("--limit", type=int, default=0, help="最多处理多少张卡")
    p.add_argument("--dry-run", action="store_true", help="只切块与估算，零成本")
    p.add_argument("--rechunk", action="store_true", help="忽略幂等，全部重算")
    p.add_argument("--price-per-1k", type=float, default=_DEFAULT_PRICE_PER_1K)
    args = p.parse_args()

    print("=" * 64)
    print(f"Backfill Chunks Alt - 固定长度对照组（size={args.size}）")
    print("=" * 64)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已中断。重跑会跳过已完成的卡片。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
