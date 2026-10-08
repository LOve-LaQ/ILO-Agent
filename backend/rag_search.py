# RAG Search - 块级检索的冒烟验证（可复现）
"""跑一批固定问题，看检索到底命中了什么、相似度多少、阈值该定在哪。

## 为什么需要这个脚本

之前那组数字（有答案 0.58~0.68 / 无答案 0.13~0.20）是**临时跑的一次性代码**产出的
—— 数字是真的，但**复现不了**。面试官说「你跑一下给我看」时手上没有能跑的东西，
比「没做」更容易被读成「数字是编的」。

所以本脚本的**首要目的不是「再测一次」，而是把那次测量固化成可复现的资产**。

## 三组问题的意义（不是随便挑的）

| 组 | 期望 | 它验证什么 |
|---|---|---|
| 有答案 | 高分命中 | 链路通不通、跨语言行不行 |
| 泛化 | 中等分 | **阈值定高的代价** —— 这类问题该不该答？ |
| 无答案 | 低分 | 拒答能不能只靠阈值做到（省一次 LLM 调用） |

第三组是最容易忽略的：只测「有答案的能命中」等于只测了召回，没测**拒答**。
而拒答测不出来，阈值就只能拍脑袋。

## 运行

    cd backend

    # 跑内置的三组固定问题（默认）
    .\\ilo\\Scripts\\python.exe rag_search.py

    # 自己的问题
    .\\ilo\\Scripts\\python.exe rag_search.py --query "Rust 的所有权是什么"

    # 看不同阈值下的拒答情况（调参用）
    .\\ilo\\Scripts\\python.exe rag_search.py --sweep

成本：每条问题一次 embedding 调用（约几十 token），八条问题的花费可忽略。
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from dotenv import load_dotenv

# 与 backfill_card_chunks 同一处理：显式锚到脚本目录，从哪运行都对。
# 无参 load_dotenv() 从**当前工作目录**往上找，从项目根跑就会找不到 .env，
# 然后所有 Key 静默为空、检索直接返回「向量算不出来」。
load_dotenv(Path(__file__).resolve().parent / ".env")

from src.core.config import settings  # noqa: E402
from src.modules.rag.chunk_store import get_chunk_store  # noqa: E402
from src.modules.rag.retriever import (  # noqa: E402
    DEFAULT_MIN_SCORE,
    DEFAULT_TOP_K,
    hydrate_texts,
    search,
)


@dataclass(frozen=True)
class Probe:
    """一条探针问题"""

    query: str
    group: str          # 有答案 / 泛化 / 无答案
    expect: str         # 人话描述期望命中什么


# 三组固定问题。**加问题要同时想清楚「它属于哪一组」** ——
# 分组的意义是让「有答案 / 无答案」的分数分布能分开看，混在一起就失去了判据。
PROBES: Tuple[Probe, ...] = (
    # ---- 有答案：语料里确实有对应内容 ----
    Probe("怎么做地震数据去噪", "有答案", "地震/去噪相关的仓库"),
    Probe("Scalpel 是做什么的", "有答案", "Scalpel 这个项目本身"),
    Probe("怎么把 QQ 机器人接到 DeepSeek", "有答案", "QQ 机器人 + DeepSeek 接入"),
    # ---- 泛化：语料里到处都有，但区分度低 ----
    Probe("怎么安装", "泛化", "任意仓库的 Installation 小节（几乎每篇都有）"),
    Probe("支持哪些大模型", "泛化", "多个仓库都会提到"),
    # ---- 无答案：语料里根本没有这类内容 ----
    Probe("今天天气怎么样", "无答案", "不该命中任何东西"),
    Probe("红烧肉怎么做", "无答案", "不该命中任何东西"),
    Probe("感冒了吃什么药", "无答案", "不该命中任何东西"),
)

# 调参扫描用的候选阈值。落在「有答案最低分」和「无答案最高分」之间的都可行。
_SWEEP_THRESHOLDS = (0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50)


async def run_query(query: str, top_k: int, min_score: float, show_text: bool) -> None:
    outcome = await search(query, top_k=top_k, min_score=min_score)

    print(f"\n{'─' * 68}")
    print(f"问题: {query}")
    print(f"原因: {outcome.reason}   最高分: {outcome.best_score:.4f}   "
          f"原始命中: {outcome.raw_hits}   过阈: {len(outcome.hits)}   "
          f"耗时: {outcome.elapsed_ms:.0f}ms")

    if not outcome.hits:
        print("  （无可用结果）")
        return

    # 回填正文 —— 命中只有坐标，正文在 PostgreSQL。
    hydrated = hydrate_texts(outcome.hits)
    for rank, item in enumerate(hydrated, start=1):
        hit = item.hit
        head = " › ".join(hit.heading_path) if hit.heading_path else "（无小节）"
        print(f"  [{rank}] {hit.score:.4f}  {hit.title or hit.card_id}")
        print(f"      {head}  chars[{hit.char_start}:{hit.char_end}]")
        if item.text is None:
            print(f"      ⚠ 取不到正文: {item.problem}")
            continue
        preview = item.text.strip().replace("\n", " ")[:110]
        print(f"      {preview}{'…' if len(item.text) > 110 else ''}")


async def run_sweep(top_k: int) -> None:
    """扫一遍阈值，看每个候选值会拒掉哪些问题。

    【为什么用「逐条重跑」而不是「一次检索多个阈值」】检索本身与阈值无关
    （阈值只在客户端后置过滤），所以其实可以只跑一次再本地套用不同阈值。
    这里仍然重跑，是因为要顺带验证**每条问题的真实耗时**；而且 embedding
    有缓存，第二次起几乎不花钱。代价可接受，换来的是数字更贴近真实链路。
    """
    print(f"\n{'=' * 68}")
    print(f"阈值扫描（top_k={top_k}）")
    print(f"{'=' * 68}")
    print(f"{'阈值':>6} | " + " | ".join(f"{p.group[:3]:>4}" for p in PROBES) + " | 通过/总数")

    scores: List[float] = []
    for probe in PROBES:
        outcome = await search(probe.query, top_k=top_k, min_score=0.0)
        scores.append(outcome.best_score)

    for threshold in _SWEEP_THRESHOLDS:
        marks = ["  ✓ " if s >= threshold else "  ✗ " for s in scores]
        passed = sum(1 for s in scores if s >= threshold)
        flag = "  ← 当前默认" if abs(threshold - DEFAULT_MIN_SCORE) < 1e-9 else ""
        print(f"{threshold:>6.2f} | " + " | ".join(marks) + f" | {passed}/{len(PROBES)}{flag}")

    print("\n各条最高分：")
    for probe, score in zip(PROBES, scores):
        print(f"  {probe.group:<4} {score:.4f}  {probe.query}")


async def run(args) -> int:
    print("=" * 68)
    print("RAG Search - 块级检索冒烟验证")
    print("=" * 68)
    print(f"Qdrant    : {settings.qdrant_url}")

    # 先确认集合里真有东西 —— 否则下面所有「无命中」都无法区分是
    # 「阈值太高」还是「库是空的」，而这两者的结论完全相反。
    try:
        store = get_chunk_store()
        total = store.count()
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ 向量库不可用: {exc}")
        print("  请确认 Qdrant 已启动，且已跑过 backfill_card_chunks.py。")
        return 1
    print(f"集合块数  : {total}")
    if total == 0:
        print("\n✗ 集合是空的。请先运行：")
        print("    .\\ilo\\Scripts\\python.exe backfill_card_chunks.py")
        return 1

    if args.query:
        await run_query(args.query, args.top_k, args.min_score, not args.no_text)
        return 0

    if args.sweep:
        await run_sweep(args.top_k)
        return 0

    started = time.monotonic()
    for probe in PROBES:
        print(f"\n【{probe.group}】期望：{probe.expect}")
        await run_query(probe.query, args.top_k, args.min_score, not args.no_text)

    # 汇总 —— 这组数字就是文档里那组「有答案 / 无答案」的分数带。
    print(f"\n{'=' * 68}")
    print("汇总（用来看「有答案」和「无答案」的分布能不能分开）")
    print(f"{'=' * 68}")
    by_group: dict[str, List[float]] = {}
    for probe in PROBES:
        outcome = await search(probe.query, top_k=args.top_k, min_score=0.0)
        by_group.setdefault(probe.group, []).append(outcome.best_score)

    for group, values in by_group.items():
        lo, hi = min(values), max(values)
        print(f"  {group:<4} n={len(values)}  最高分 {lo:.4f} ~ {hi:.4f}")

    if "有答案" in by_group and "无答案" in by_group:
        answer_floor = min(by_group["有答案"])
        noise_ceiling = max(by_group["无答案"])
        gap = answer_floor - noise_ceiling
        print(f"\n  有答案下界 {answer_floor:.4f} − 无答案上界 {noise_ceiling:.4f} "
              f"= 间隔带 {gap:.4f}")
        if gap > 0:
            print(f"  → 分隔干净：阈值取 ({noise_ceiling:.2f}, {answer_floor:.2f}) 之间都行，"
                  f"默认 {DEFAULT_MIN_SCORE}")
        else:
            print("  → ⚠ 两组重叠了：说明光靠相似度阈值分不开，需要重排或更好的向量模型")

    print(f"\n总耗时 {time.monotonic() - started:.1f}s")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="块级检索冒烟验证（card_chunks）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--query", default="", help="只跑这一条问题（不跑内置的三组）")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K,
                        help=f"取几条，默认 {DEFAULT_TOP_K}")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE,
                        help=f"拒答阈值，默认 {DEFAULT_MIN_SCORE}")
    parser.add_argument("--sweep", action="store_true",
                        help="扫一遍候选阈值，看各自拒掉哪些问题")
    parser.add_argument("--no-text", action="store_true",
                        help="不回填正文（只看分数和坐标，更快）")
    args = parser.parse_args()

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
