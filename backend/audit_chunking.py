# Audit Chunking - 切分器体检（只读，零成本）
"""拿真实语料验证切分器：块数对不对、块大小分布合不合理、区间不变式有没有破。

## 为什么要在入库之前单独体检

切分错了，后面全部白做：块向量是照着切出来的块算的，评测集是照着块的区间标的。
而切分的问题在真实语料上才暴露得出来 —— 单测用的是构造文本，真实 README 里有
未闭合的代码围栏、超长的单段落、h5/h6 的深嵌套。

## 检查四件事

1. **块数** —— 与 `audit_corpus_for_rag.py` 的预估是否同量级（预估按 h1-h2 切约 14,128）
2. **块核心大小分布** —— 有没有大量贴边的碎块，或大量超长块
3. **区间不变式** —— `text == 原文[char_start:char_end]`，逐块校验。这条破了，
   任务 B 的评测就做不出来，且不会有任何报错，只会静默出错
4. **参数敏感性** —— 同一份语料换 `max_chars` 重跑，看块数怎么变。这组对应关系
   是后面调参的起点，省得每次重新摸

## 尺寸为什么要分两个口径

`max_chars` 约束的是**块的核心长度**，而入库的 `text` 还额外带了前一块的尾部重叠
（默认 15%）。拿带重叠的 `text` 去比 `max_chars`，会把正常块误判成超长 —— 所以本
脚本的尺寸统计走一遍 `overlap_ratio=0`，重叠开销单独报一行。

## 运行

    cd backend
    .\\ilo\\Scripts\\python.exe audit_chunking.py                # 默认参数全量体检
    .\\ilo\\Scripts\\python.exe audit_chunking.py --sweep        # 附参数敏感性表
    .\\ilo\\Scripts\\python.exe audit_chunking.py --limit 50     # 小样本先看

## 成本

零。不调 embedding、不写库、不碰 LLM。
"""

from __future__ import annotations

import argparse
import os
import statistics
import time
from collections import Counter

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from src.modules.rag.chunker import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_HEADING_LEVEL,
    DEFAULT_MIN_CHARS,
    DEFAULT_OVERLAP_RATIO,
    chunk_markdown,
)

load_dotenv()

FETCH = text("""
    select item_id, raw_content
      from collection_records
     where raw_content is not null and raw_content <> ''
     order by item_id
""")

# 低于这个长度的块会被 `_merge_short` 尽力并掉；并不掉的比例低于阈值就不算问题
_FRAGMENT_RATIO_WARN = 0.005


def _load_documents(limit: int | None) -> list[tuple[str, str]]:
    engine = create_engine(os.getenv("DATABASE_URL"))
    with engine.connect() as conn:
        rows = list(conn.execute(FETCH).mappings())
    if limit:
        rows = rows[:limit]
    return [(r["item_id"], r["raw_content"]) for r in rows]


def _chunk_all(docs, *, max_chars, overlap_ratio, min_chars, max_heading_level):
    """切分全部文档，顺带做区间不变式自检

    Returns:
        (每篇的块列表, 不变式违例清单)
    """
    per_doc: list[list[dict]] = []
    violations: list[tuple[str, int]] = []

    for item_id, raw in docs:
        chunks = chunk_markdown(
            raw,
            max_chars=max_chars,
            overlap_ratio=overlap_ratio,
            min_chars=min_chars,
            max_heading_level=max_heading_level,
        )
        for chunk in chunks:
            if chunk["text"] != raw[chunk["char_start"] : chunk["char_end"]]:
                violations.append((item_id, chunk["ordinal"]))
        per_doc.append(chunks)

    return per_doc, violations


def _size_stats(per_doc: list[list[dict]]) -> list[int]:
    """块核心大小（不含重叠）—— `max_chars` 约束的就是它"""
    return [chunk["char_end"] - chunk["char_start"] for chunks in per_doc for chunk in chunks]


def _overlap_overhead(per_doc_with_overlap, per_doc_core) -> float:
    """平均每块因为重叠多出来的字符数"""
    with_len = sum(len(c["text"]) for chunks in per_doc_with_overlap for c in chunks)
    core_len = sum(len(c["text"]) for chunks in per_doc_core for c in chunks)
    total = sum(len(chunks) for chunks in per_doc_with_overlap)
    return (with_len - core_len) / total if total else 0.0


def _print_report(
    *,
    docs,
    per_doc,
    per_doc_core,
    violations,
    max_chars: int,
    min_chars: int,
    elapsed: float,
) -> dict:
    counts = [len(c) for c in per_doc]
    total = sum(counts)
    sizes = sorted(_size_stats(per_doc_core))

    print()
    print("=" * 68)
    print(f"切分体检（max_chars={max_chars}，min_chars={min_chars}）")
    print("=" * 68)
    print(f"  文档数              {len(docs)}")
    print(f"  总字符              {sum(len(raw) for _, raw in docs):,}")
    print(f"  总块数              {total:,}")
    print(f"  每篇块数            中位数 {statistics.median(counts):.0f}  最大 {max(counts)}")
    print(f"  切不动的文档        {sum(1 for n in counts if n == 0)}")

    print()
    print("  块核心大小（字符，不含重叠）")
    print(f"    最小 {sizes[0]}   中位 {sizes[len(sizes) // 2]}   "
          f"P90 {sizes[int(len(sizes) * 0.9)]}   最大 {sizes[-1]}")
    print(f"    重叠开销            平均每块 +{_overlap_overhead(per_doc, per_doc_core):.0f} 字符")

    buckets: Counter = Counter()
    for n in sizes:
        if n < 100:
            buckets["<100（碎）"] += 1
        elif n < 500:
            buckets["100-500"] += 1
        elif n < 2000:
            buckets["500-2k"] += 1
        elif n <= max_chars:
            buckets["2k-上限"] += 1
        else:
            buckets["超上限"] += 1
    print()
    for label in ["<100（碎）", "100-500", "500-2k", "2k-上限", "超上限"]:
        n = buckets.get(label, 0)
        if not n:
            continue
        print(f"    {label:>12}  {n:>7}  {n / len(sizes) * 100:5.1f}%  "
              f"{'#' * int(n / len(sizes) * 50)}")

    print()
    print("  区间不变式（text == 原文[char_start:char_end]）")
    if violations:
        print(f"    ❌ 违例 {len(violations)} 处，前 5 条：{violations[:5]}")
        print("       → 必须先修，否则任务 B 的评测标签无法机械对齐")
    else:
        print(f"    ✅ {total:,} 块全部通过")

    print(f"\n  耗时 {elapsed:.1f}s")

    return {"total": total, "sizes": sizes, "buckets": buckets, "docs": len(docs)}


def main() -> None:
    parser = argparse.ArgumentParser(description="切分器体检（只读，零成本）")
    parser.add_argument("--limit", type=int, default=0, help="只取前 N 篇（0 = 全量）")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--overlap", type=float, default=DEFAULT_OVERLAP_RATIO)
    parser.add_argument("--min-chars", type=int, default=DEFAULT_MIN_CHARS)
    parser.add_argument("--max-heading-level", type=int, default=DEFAULT_MAX_HEADING_LEVEL)
    parser.add_argument("--sweep", action="store_true", help="附参数敏感性表")
    args = parser.parse_args()

    docs = _load_documents(args.limit or None)
    if not docs:
        print("语料为空：先跑一次采集或 backfill_repos_30d.py 再体检。")
        return

    print(f"载入 {len(docs)} 篇文档")

    started = time.monotonic()
    # 带重叠：这一版才是真正会入库的，不变式必须对它校验
    per_doc, violations = _chunk_all(
        docs,
        max_chars=args.max_chars,
        overlap_ratio=args.overlap,
        min_chars=args.min_chars,
        max_heading_level=args.max_heading_level,
    )
    # 不带重叠：用于尺寸统计，否则重叠会被误算成超长
    per_doc_core, core_violations = _chunk_all(
        docs,
        max_chars=args.max_chars,
        overlap_ratio=0.0,
        min_chars=args.min_chars,
        max_heading_level=args.max_heading_level,
    )
    elapsed = time.monotonic() - started

    stat = _print_report(
        docs=docs,
        per_doc=per_doc,
        per_doc_core=per_doc_core,
        violations=violations + core_violations,
        max_chars=args.max_chars,
        min_chars=args.min_chars,
        elapsed=elapsed,
    )

    if args.sweep:
        print()
        print("=" * 68)
        print("参数敏感性（只换 max_chars，其余不变）")
        print("=" * 68)
        print(f"  {'max_chars':>10}  {'块数':>9}  {'均块/篇':>8}  {'相对变化':>9}  {'超上限':>8}")
        for value in [400, 800, 1200, 2000, 3000]:
            swept, _ = _chunk_all(
                docs,
                max_chars=value,
                overlap_ratio=0.0,
                min_chars=args.min_chars,
                max_heading_level=args.max_heading_level,
            )
            sizes = _size_stats(swept)
            total = sum(len(c) for c in swept)
            oversize = sum(1 for n in sizes if n > value)
            delta = (total - stat["total"]) / stat["total"] * 100
            marker = " ←当前" if value == args.max_chars else ""
            print(f"  {value:>10}  {total:>9,}  {total / len(docs):>8.1f}  "
                  f"{delta:>+8.1f}%  {oversize:>8,}{marker}")

    print()
    print("=" * 68)
    print("判断")
    print("=" * 68)
    per_doc_avg = stat["total"] / stat["docs"] if stat["docs"] else 0

    # 小样本时总块数天然到不了全量区间，先按篇均值判，才不会被样本量带偏
    if 8 <= per_doc_avg <= 25:
        print(f"  ✅ 每篇块数 {per_doc_avg:.1f} 落在合理区间 8~25")
    else:
        print(f"  ⚠ 每篇块数 {per_doc_avg:.1f} 落在 8~25 之外，先查切分逻辑")

    if stat["docs"] >= 900:
        if 10_000 <= stat["total"] <= 20_000:
            print(f"  ✅ 总块数 {stat['total']:,} 落在预期区间 10,000~20,000")
        else:
            print(f"  ⚠ 总块数 {stat['total']:,} 落在预期区间 10,000~20,000 之外")

    fragments = stat["buckets"].get("<100（碎）", 0)
    fragment_ratio = fragments / stat["total"] if stat["total"] else 0

    if violations:
        print("  ❌ 区间不变式有违例 —— 修完再进 P2")
    elif fragment_ratio > _FRAGMENT_RATIO_WARN:
        print(f"  ⚠ 碎块占比 {fragment_ratio * 100:.1f}% 偏高，检查 min_chars 是否需要调大")
    elif stat["sizes"][-1] > args.max_chars * 1.5:
        print(f"  ⚠ 最大块 {stat['sizes'][-1]} 字超过上限的 1.5 倍，检查围栏避让逻辑")
    else:
        print(f"  ✅ 无碎块异常（{fragments} 个并不掉的短块）、无超长块")


if __name__ == "__main__":
    main()
