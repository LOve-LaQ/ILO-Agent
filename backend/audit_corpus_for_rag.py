# Audit Corpus For RAG - 语料体检（只读）
"""给 RAG 改造摸底：切分策略可不可行、入库成本多大、检索会不会踩跨语言的坑。

## 为什么需要这个脚本

`docs/interview/02-rag.md` §三 的改造清单给了方案，但方案里的关键参数（切多长、
重叠多少、取几条）**必须由语料本身决定**，不能拍脑袋。这个脚本把「决定参数所需
的事实」一次量出来，跑完再动手，避免做到一半发现语料形态和假设不符。

## 回答四个问题

1. **有多少料** —— 篇数、长度分布。太短则切分无意义，太长则必须二次切。
2. **有没有结构** —— Markdown 标题层级分布。有结构就优先按结构切（语义边界是
   作者划好的），没结构才退到长度兜底。
3. **要建多少块** —— 不同策略下的预估块数。直接决定入库耗时与花钱量。
4. **会不会跨语言** —— 原文语言 vs 摘要语言。中文提问检索英文原文，是向量检索
   最容易静默失效的地方（相似度看起来正常，召回全是错的）。

## 运行

    cd backend
    .\\ilo\\Scripts\\python.exe audit_corpus_for_rag.py

## 只读保证

全部是 SELECT，不写库、不调 LLM、不调 embedding，零成本。
"""

import os
import re
from collections import Counter

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

FETCH = text("""
    select item_id,
           raw_content,
           card_payload->>'summary'   as summary,
           card_payload->>'one_liner' as one_liner
      from collection_records
     where raw_content is not null and raw_content <> ''
""")

OVERVIEW = text("""
    select count(*)                          as 记录总数,
           count(raw_content)                as 有原文,
           count(content_digest_zh)          as 有中文导读,
           count(*) filter (where source_platform = 'github') as github来源,
           round(avg(length(raw_content)))   as 原文均长,
           percentile_cont(0.5) within group (order by length(raw_content)) as 原文中位数,
           percentile_cont(0.9) within group (order by length(raw_content)) as 原文P90
      from collection_records
""")

HEADING = re.compile(r"^(#{1,6})\s+\S", re.MULTILINE)
CJK = re.compile(r"[\u4e00-\u9fff]")

_LEN_BUCKETS = [
    (500, "<500"),
    (2000, "500-2k"),
    (8000, "2k-8k"),
    (30000, "8k-30k"),
    (float("inf"), ">30k"),
]


def _bucket_of(length: int) -> str:
    for upper, label in _LEN_BUCKETS:
        if length < upper:
            return label
    return ">30k"


def _rule(title: str) -> None:
    print()
    print("=" * 66)
    print(title)
    print("=" * 66)


def _est_by_heading(body: str, max_level: int, second_pass: int = 1600) -> int:
    """按 <=max_level 的标题切；单块超长（>2000 字）再按长度二次切。

    这是对 chunker 的**粗略模拟**，只为估量级，不作为实现依据。
    """
    pattern = re.compile(r"^(#{1,%d})\s+\S" % max_level, re.MULTILINE)
    parts = pattern.split(body)
    # re.split 带捕获组时正文落在偶数下标：0,2,4...
    chunks = 0
    for i in range(0, len(parts), 2):
        piece = parts[i]
        if not piece.strip():
            continue
        chunks += 1 if len(piece) <= 2000 else -(-len(piece) // second_pass)
    return max(chunks, 1)


def _est_by_length(body: str, size: int) -> int:
    return max(1, -(-len(body) // size))


def main() -> None:
    engine = create_engine(os.getenv("DATABASE_URL"))

    with engine.connect() as conn:
        overview = conn.execute(OVERVIEW).mappings().one()
        rows = list(conn.execute(FETCH).mappings())

    _rule("1. 语料总览")
    for key, value in overview.items():
        print(f"  {key:<12} {value}")

    if not rows:
        print("\n  语料为空：先跑一次采集或 backfill_repos_30d.py 再体检。")
        return

    lengths = [len(r["raw_content"]) for r in rows]

    _rule("2. 原文长度分布（字符）")
    counter = Counter(_bucket_of(n) for n in lengths)
    for _, label in _LEN_BUCKETS:
        n = counter.get(label, 0)
        if not n:
            continue
        pct = n / len(rows) * 100
        print(f"  {label:>8}  n={n:<5} {pct:5.1f}%  {'#' * int(pct / 2)}")
    print(f"\n  合计 {len(rows)} 篇，总字符 {sum(lengths):,}")

    _rule("3. Markdown 标题层级分布")
    level_counts: Counter = Counter()
    per_doc: list = []
    for r in rows:
        heads = HEADING.findall(r["raw_content"])
        per_doc.append(len(heads))
        for h in heads:
            level_counts[len(h)] += 1

    total_heads = sum(level_counts.values())
    for lv in sorted(level_counts):
        n = level_counts[lv]
        print(f"  h{lv}  {n:>7}  {n / total_heads * 100:5.1f}%")
    per_doc.sort()
    n_doc = len(per_doc)
    print(f"\n  每篇标题数：中位数 {per_doc[n_doc // 2]}  "
          f"P10 {per_doc[n_doc // 10]}  P90 {per_doc[n_doc * 9 // 10]}  "
          f"最大 {per_doc[-1]}")
    print(f"  全库标题总数：{total_heads:,}")
    zero = sum(1 for x in per_doc if x == 0)
    print(f"  零标题（只能按长度兜底）：{zero} / {n_doc}  "
          f"({zero / n_doc * 100:.1f}%)")

    _rule("4. 不同切分策略的预估块数（粗估，仅用于估量级）")
    strategies = [
        ("按 h1-h2 切 + 超长二次切", lambda t: _est_by_heading(t, 2)),
        ("按 h1-h3 切 + 超长二次切", lambda t: _est_by_heading(t, 3)),
        ("固定长度 400 字", lambda t: _est_by_length(t, 400)),
        ("固定长度 800 字", lambda t: _est_by_length(t, 800)),
        ("固定长度 1200 字", lambda t: _est_by_length(t, 1200)),
    ]
    for label, fn in strategies:
        total = sum(fn(r["raw_content"]) for r in rows)
        print(f"  {label:<26} 约 {total:>7,} 块   （均 {total / len(rows):.1f} 块/篇）")

    _rule("5. 语言构成（CJK 字符占比）—— 跨语言检索风险")

    def cjk_ratio(t: str) -> float:
        return len(CJK.findall(t or "")) / max(len(t or ""), 1)

    def lang_bucket(v: float) -> str:
        if v < 0.01:
            return "纯英文 (<1%)"
        if v < 0.05:
            return "英文为主 (1-5%)"
        if v < 0.30:
            return "中英混合 (5-30%)"
        return "中文为主 (>30%)"

    raw_ratios = [cjk_ratio(r["raw_content"]) for r in rows]
    sum_ratios = [cjk_ratio(r["summary"]) for r in rows]

    for label, ratios in [("README 原文", raw_ratios), ("现有 summary", sum_ratios)]:
        print(f"  【{label}】")
        for name, n in Counter(lang_bucket(x) for x in ratios).most_common():
            print(f"    {name:<18} {n:>5} 条  {n / len(rows) * 100:5.1f}%")
        print(f"    平均 CJK 占比 {(sum(ratios) / len(ratios)) * 100:.2f}%")
        print()

    raw_avg = sum(raw_ratios) / len(raw_ratios)
    sum_avg = sum(sum_ratios) / len(sum_ratios)

    _rule("6. 结论")
    if raw_avg < 0.30 and sum_avg > 0.30:
        print("  ⚠ 原文以英文为主，摘要以中文为主 → **存在跨语言检索问题**。")
        print("    中文提问要命中英文原文块，要求向量模型在中文与英文之间对齐。")
        print("    → 评测集必须包含「中文提问 / 英文原文」这一类样本；")
        print("    → 「纯向量 vs 混合检索」的差距在这类语料上通常被放大，务必实测。")
    else:
        print("  原文与摘要语言基本一致，跨语言风险较低。")
    print(f"  语料 {len(rows)} 篇 / 标题 {total_heads:,} 个 / 总字符 {sum(lengths):,}")


if __name__ == "__main__":
    main()
