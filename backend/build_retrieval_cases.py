# Build Retrieval Cases - 检索评测集的标注脚手架
"""把「人工/智能体标注」这件事变成**可复现的两步**：先出题面，再把答案锚定到字符区间。

## 为什么需要脚手架，而不是直接手写一个 JSON

评测集的标签必须是**原文的字符区间**，不能是块编号 —— 块编号会随切分参数变，
改一次 `max_chars` 整个集就废了（见 `docs/interview/02-rag.md` §3.7）。
但手写字符偏移量既易错又没法复核。所以拆成两步：

1. **`--dump`**：抽样卡片、切块、把「候选题面」导出成一张工作单（带 card_id 和块的
   字符区间）。标注者（人或智能体）读题面，写出问题，并**从题面里原样复制一句
   「答案原句」**。
2. **`--resolve`**：把草稿里的「答案原句」在 `raw_content` 里**唯一定位**，算出它的
   精确字符区间，产出最终 `retrieval_cases.json`。

这样标签的正确性由脚本保证（原句必须真实存在且唯一），标注者只需要负责
「问题问得对不对」这一件人该做的事。

## 标注口径（写进产物的 `labeling` 字段，面试时要说）

本集是 **agent-grounded（有依据的自动标注）**：问题由标注者**阅读真实原文后**
按具体段落写出，答案区间锚定原文偏移。它**不是**独立第三方人工标注，
所以跑出来的命中率应视为**乐观上界**，不是绝对指标。

面试时的正确说法：「评测集是我按原文段落逐条出的题、答案锚定字符区间，
属于有依据的自动标注，所以命中率偏乐观；它足够用来做**策略之间的对照**
（按标题切 vs 固定长度），但不足以宣称绝对水平。」

## 运行

    cd backend

    # 1) 出题面（只读 DB，零成本）
    .\\ilo\\Scripts\\python.exe build_retrieval_cases.py --dump --per-bucket 10

    # 2) 标注完草稿后，定位答案区间并生成评测集
    .\\ilo\\Scripts\\python.exe build_retrieval_cases.py --resolve

## 只读保证

`--dump` 全是 SELECT，不调 LLM、不调 embedding、不写库。
`--resolve` 只读 DB + 写一个 JSON 文件。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from sqlalchemy import create_engine, text  # noqa: E402

from src.modules.rag.chunker import chunk_markdown  # noqa: E402

_HERE = Path(__file__).resolve().parent
WORKSHEET_PATH = _HERE / "retrieval_worksheet.md"
DRAFT_PATH = _HERE / "retrieval_cases.draft.json"
CASES_PATH = _HERE / "retrieval_cases.json"

CJK = re.compile(r"[\u4e00-\u9fff]")

# 抽样的桶。语料里 77% 是纯英文 README，所以「中文提问 → 英文原文」是主战场；
# 但评测集必须**分层**，否则整体命中率会被大桶淹没，看不出跨语言的真实差距。
BUCKETS = ("en", "mixed", "zh")

FETCH = text("""
    select item_id, raw_content, source_url,
           card_payload->>'title' as title
      from collection_records
     where raw_content is not null and raw_content <> ''
       and length(raw_content) >= 3000
     order by item_id
""")


def _cjk_ratio(t: str) -> float:
    return len(CJK.findall(t or "")) / max(len(t or ""), 1)


def _bucket_of(raw: str) -> str:
    r = _cjk_ratio(raw)
    if r < 0.01:
        return "en"
    if r < 0.30:
        return "mixed"
    return "zh"


def _load_cards() -> List[Dict[str, Any]]:
    engine = create_engine(os.getenv("DATABASE_URL"))
    with engine.connect() as conn:
        rows = list(conn.execute(FETCH).mappings())
    cards: List[Dict[str, Any]] = []
    for r in rows:
        raw = r["raw_content"] or ""
        cards.append(
            {
                "card_id": str(r["item_id"]),
                "raw_content": raw,
                "source_url": r["source_url"] or "",
                "title": str(r["title"] or ""),
                "bucket": _bucket_of(raw),
            }
        )
    return cards


# 剔掉 URL / 图片标签 / 代码围栏 / markdown 链接语法之后，剩多少「像人话」的字符。
# 这个数太低说明这块基本是链接清单或代码 —— 出不了「读懂正文才能答」的题，
# 只能出「装了什么依赖」这种靠关键词撞的题，那种题会把命中率虚高。
_URL = re.compile(r"https?://\S+")
_IMG = re.compile(r"<img[^>]*>")
_MDLINK = re.compile(r"\[[^\]]*\]\([^)]*\)")
_FENCE = re.compile(r"^\s*```.*$", re.MULTILINE)
_BACKTICK = re.compile(r"`[^`]*`")


def _prose_chars(text: str) -> int:
    t = _FENCE.sub("", text)
    t = _IMG.sub("", t)
    t = _MDLINK.sub("", t)
    t = _URL.sub("", t)
    t = _BACKTICK.sub("", t)
    t = re.sub(r"[|>*#\-\[\]()\s]+", "", t)
    return len(t)


def _pick_chunks(raw: str) -> List[Dict[str, Any]]:
    """切块，挑出「适合出题」的块。

    适合出题 = 长度 400~1400 字符 **且** 剔掉 URL/代码后仍有 >= 260 字符正文。
    太短的块信息量不够；太长的块答案分散、标注区间会很大；链接清单/纯代码块
    出不了需要「读懂正文」的题 —— 那种题靠关键词就能撞上，会把命中率虚高。
    """
    chunks = chunk_markdown(raw)
    picked: List[Dict[str, Any]] = []
    for c in chunks:
        span = c["char_end"] - c["char_start"]
        if not (400 <= span <= 1400):
            continue
        if _prose_chars(c["text"]) < 260:
            continue
        picked.append(c)
    return picked


# 「awesome 清单」类仓库不适合出题：正文几乎全是论文/项目链接，问什么都只能靠
# 关键词撞。语料里有十几篇，抽样时先剔掉。
_AWESOME = re.compile(r"awesome", re.IGNORECASE)


def dump_worksheet(per_bucket: int, seed: int) -> None:
    cards = _load_cards()
    by_bucket: Dict[str, List[Dict[str, Any]]] = {b: [] for b in BUCKETS}
    skipped_awesome = 0
    for c in cards:
        if _AWESOME.search(c["title"]):
            skipped_awesome += 1
            continue
        by_bucket[c["bucket"]].append(c)

    rng = random.Random(seed)
    lines: List[str] = []
    lines.append("# 检索评测集 · 出题工作单\n")
    lines.append(f"> 语料 {len(cards)} 篇（有 README 且 >= 3000 字符）。")
    lines.append(
        "> 分层：" + " / ".join(f"{b}={len(by_bucket[b])}" for b in BUCKETS) + "\n"
    )
    lines.append(
        "> **怎么用**：读下面的题面，为每个槽位写一条**中文问题**，"
        "并从题面里**原样复制一句「答案原句」**（20~80 字符，必须唯一）"
        "填进 `retrieval_cases.draft.json`。\n"
    )

    slot = 0
    stats = Counter()
    used_cards = 0
    for bucket in BUCKETS:
        pool = by_bucket[bucket]
        if not pool:
            continue
        rng.shuffle(pool)
        emitted = 0
        for card in pool:
            if emitted >= per_bucket:
                break
            picked = _pick_chunks(card["raw_content"])
            if not picked:
                continue
            # **一张卡只取一块**：同一张卡的两块之间可能有 15% 重叠，会产出
            # 高度相似的题，让评测集看起来条数很多、实际信息量很少。
            # 取靠中间的那一块 —— 文档开头的块常是「项目简介」，问题容易撞上
            # 仓库名和一句话 tagline，那靠标题就能命中，测不出正文检索。
            ch = picked[len(picked) // 2]
            slot += 1
            emitted += 1
            used_cards += 1
            stats[bucket] += 1
            span_len = ch["char_end"] - ch["char_start"]
            lines.append(f"## 槽位 {slot}  ·  桶 `{bucket}`\n")
            lines.append(f"- card_id: `{card['card_id']}`")
            lines.append(f"- title: {card['title'] or '(无)'}")
            lines.append(f"- url: {card['source_url']}")
            lines.append(
                f"- 本卡候选块 {len(picked)} 个；本槽位取其中第 {len(picked) // 2 + 1} 个"
                f" ｜ 全文块序号 ordinal {ch['ordinal']}"
                f" ｜ span [{ch['char_start']}, {ch['char_end']}) ｜ {span_len} 字符"
            )
            lines.append(
                f"- 标题路径: {' › '.join(ch['heading_path']) or '(无)'}"
            )
            lines.append("\n```text")
            lines.append(ch["text"])
            lines.append("```\n")
        if emitted == 0:
            lines.append(f"> 桶 `{bucket}` 没有可用卡片。\n")

    WORKSHEET_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"✓ 工作单已写出: {WORKSHEET_PATH}")
    print(f"  槽位 {slot} 个 / 用了 {used_cards} 张卡"
          f"（" + " / ".join(f"{b}={stats[b]}" for b in BUCKETS) + "）")
    if skipped_awesome:
        print(f"  跳过 awesome 清单类 {skipped_awesome} 篇")
    print("  下一步：按工作单写 `retrieval_cases.draft.json`，再跑 --resolve")


def _locate(raw: str, excerpt: str) -> Tuple[int, int]:
    """在原文里**唯一**定位答案原句，返回 `(start, end)`。"""
    idx = raw.find(excerpt)
    if idx < 0:
        raise ValueError("原句在原文中找不到（可能含换行/空格差异）")
    if raw.find(excerpt, idx + 1) >= 0:
        raise ValueError("原句在原文中出现了多次，不唯一 —— 请换一句更长的")
    return idx, idx + len(excerpt)


def resolve_draft() -> None:
    if not DRAFT_PATH.exists():
        print(f"✗ 找不到草稿: {DRAFT_PATH}")
        print("  先按工作单写出草稿（字段见 --dump 的说明）。")
        raise SystemExit(1)

    draft = json.loads(DRAFT_PATH.read_text(encoding="utf-8"))
    cards = {c["card_id"]: c for c in _load_cards()}

    cases: List[Dict[str, Any]] = []
    problems: List[str] = []
    for i, item in enumerate(draft.get("cases", []), start=1):
        cid = str(item.get("card_id", ""))
        card = cards.get(cid)
        if card is None:
            problems.append(f"#{i} card_id={cid} 不在语料里")
            continue
        try:
            start, end = _locate(card["raw_content"], item["excerpt"])
        except ValueError as e:
            problems.append(f"#{i} card_id={cid}: {e}")
            continue

        # 自检：答案区间必须完整落在**某一个已入库的块**里。
        # 否则这条标签在物理上就不可能被命中，会人为拉低命中率。
        chunks = chunk_markdown(card["raw_content"])
        covering = [
            c for c in chunks if c["char_start"] <= start and c["char_end"] >= end
        ]
        if not covering:
            problems.append(
                f"#{i} card_id={cid}: 答案区间 [{start},{end}) 跨越了块边界，"
                f"无法被单块完整命中 —— 请换一句更短的（块内）原句"
            )
            continue

        cases.append(
            {
                "id": f"c{i:03d}",
                "kind": "positive",
                "query": item["query"],
                "card_id": cid,
                "repo": card["title"],
                "lang_pair": item.get("lang_pair")
                or f"zh_query_{card['bucket']}_doc",
                "difficulty": item.get("difficulty", "detail"),
                "answer_span": [start, end],
                "answer_excerpt": item["excerpt"],
                "covering_chunk_ordinals": [c["ordinal"] for c in covering],
                "note": item.get("note", ""),
            }
        )

    if problems:
        print(f"✗ {len(problems)} 条草稿有问题，未生成评测集：")
        for p in problems:
            print(f"  - {p}")
        raise SystemExit(1)

    # 负样本（语料里根本没有答案的问题）。**必须单独成组**：
    # 没有它们，「命中率」只能说明「能召回」，说明不了「该拒答时会不会乱答」。
    # 它们不给答案区间 —— 正确的行为是**一条都不返回**（best_score 低于阈值）。
    negatives: List[Dict[str, Any]] = []
    for j, item in enumerate(draft.get("negatives", []), start=1):
        negatives.append(
            {
                "id": f"n{j:03d}",
                "kind": "negative",
                "query": item["query"],
                "why": item.get("why", ""),
                "answer_span": None,
            }
        )

    payload = {
        "version": 1,
        "created": "2026-10-08",
        "chunker": {"max_chars": 2000, "overlap_ratio": 0.15, "min_chars": 100},
        "labeling": {
            "method": "agent-grounded",
            "by": "coding agent（阅读真实原文后出题，答案锚定原文字符区间）",
            "not_human": True,
            "caveat": (
                "问题按真实段落写出、答案区间由脚本唯一定位，但**不是独立第三方人工标注**。"
                "命中率应视为**乐观上界**：出题者读过原文，可能无意识地把问题写得"
                "更贴近原文用词。它足够支撑**策略间对照**（按标题切 vs 固定长度），"
                "不足以宣称绝对水平。"
            ),
            "negatives": (
                "负样本是**语料外的常识问题**（天气 / 菜谱 / 电影…）。它们没有答案区间，"
                "正确行为是 best_score 低于阈值、一条都不返回。用来量「该拒答时会不会乱答」。"
            ),
        },
        "metrics": {
            "hit@k": "前 k 条命中里，存在一个块的区间**完整包含** answer_span",
            "recall@k": "前 k 条命中的区间并集覆盖 answer_span 的比例",
            "mrr": "第一个包含答案的命中所在名次的倒数",
            "false_accept": "负样本的 best_score >= 阈值 —— 本该拒答却给了资料",
        },
        "cases": cases,
        "negatives": negatives,
    }
    CASES_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    dist = Counter(c["lang_pair"] for c in cases)
    print(f"✓ 评测集已写出: {CASES_PATH}")
    print(f"  正样本 {len(cases)} 条 / 负样本 {len(negatives)} 条")
    for k, v in sorted(dist.items()):
        print(f"    {k:<22} {v}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检索评测集标注脚手架（出题面 / 定位答案区间）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dump", action="store_true", help="导出出题工作单（只读）")
    parser.add_argument("--resolve", action="store_true", help="读草稿，定位区间，生成评测集")
    parser.add_argument("--per-bucket", type=int, default=8, help="每个语言桶抽几张卡")
    parser.add_argument("--seed", type=int, default=20261008, help="抽样随机种子")
    args = parser.parse_args()

    if args.dump:
        dump_worksheet(args.per_bucket, args.seed)
        return 0
    if args.resolve:
        resolve_draft()
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
