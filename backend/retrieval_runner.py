# Retrieval Runner - 检索评测执行器
"""拿 `retrieval_cases.json` 跑一遍真实检索，算出命中率并出报告。

## 它回答的问题

1. **命中率**：答案所在的那一块，有没有进前 k 名？（`hit@k`）
2. **排得多靠前**：第一个包含答案的命中排第几？（`MRR`）
3. **该拒答时会不会乱答**：语料外的问题，`best_score` 有没有越过阈值？（`false_accept`）
4. **哪一类最难**：按 `lang_pair`（中问英答 / 中问中答 / 中问越答）和
   `difficulty` 分组看，差距在哪。

## 两个必须分开的口径

- **排序指标**（hit@k / recall@k / MRR）用 `min_score=0.0` 跑 —— 它们衡量的是
  「检索排得对不对」，阈值会把排在后面的正确块提前砍掉，混进来就测不准。
- **拒答指标**（false_accept）用真实阈值跑 —— 它衡量的是「阈值定得对不对」。

这两个数混在一个口径里，会出现「阈值调高 → 命中率下降 → 看起来检索变差」的
假象，其实只是砍得更狠。**分开测，才能分别调。**

## 标签的性质（报告里会写明）

评测集是 **agent-grounded**（读原文后出题、答案锚定字符区间），
不是独立人工标注 —— 所以命中率是**乐观上界**。
它足以支撑**策略间对照**，不足以宣称绝对水平。报告里必须带上这句，
否则这个数字会被当成「真实用户满意度」。

## 运行

    cd backend

    # 默认集合（按标题切）
    .\\ilo\\Scripts\\python.exe retrieval_runner.py

    # 换集合做策略对照
    .\\ilo\\Scripts\\python.exe retrieval_runner.py --collection card_chunks_fixed800 --tag fixed800

    # 两套策略配对比（离线，不调 API）
    .\\ilo\\Scripts\\python.exe retrieval_runner.py --compare heading fixed800

## 对比模式（`--compare`）为什么不能只比总命中率

总命中率把「两侧都命中」的样本也算进去，会**稀释真实差异**。所以对比做三件事：

1. **配对符号检验**（精确二项）：只看「一边命中、另一边没命中」的 discordant 对，
   问「翻转方向是不是随机的」。**聚合赢 ≠ 显著** —— 实测按标题切 5 项赢 4 项，
   但 n=36 时 p=0.55，不显著。这时候**不能宣布门禁通过**。
2. **配对 t 检验**（逐条 RR 差值）：用上「差多远」这个幅度信息，补符号检验只看方向的不足。
3. **召回层 vs 排序层拆分**：`hit@k` 把「召回到了没」和「排得靠前没」混在一起。
   拆开才发现两套切法**召回层完全打平（都是 32/36）**、差异全在排序 ——
   **这一条直接把下一步动作从「换切分器」改成了「加重排」**。

## 成本

每条问题一次 embedding 调用。36 + 10 = 46 条约几分钱。Qdrant 本地。
`--compare` 纯离线读 JSON，零成本。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")

from src.modules.rag.retriever import (  # noqa: E402
    DEFAULT_MIN_SCORE,
    DEFAULT_TOP_K,
    REASON_OK,
    ChunkHit,
    RetrievalOutcome,
    search,
)

CASES_PATH = _HERE / "retrieval_cases.json"

# 报告里要出现的 k。10 是「给重排留的候选量级」，前三个是「直接塞提示词」的档位。
K_LIST = (1, 3, 5, 10)


# ==================== 指标 ====================


def _contained(hit: ChunkHit, span: Sequence[int]) -> bool:
    """块的区间是否**完整包含**答案区间

    用包含而不是重叠：重叠会把「答案跨在两块交界、各拿一半」也算成命中，
    而那种情况模型拿到的信息是残缺的。包含是更严、也更贴近「能答对」的定义。
    """
    return hit.char_start <= span[0] and hit.char_end >= span[1]


def _overlap_ratio(hit: ChunkHit, span: Sequence[int]) -> float:
    """块与答案区间的重叠占答案区间的比例（诊断用，不参与判定）"""
    lo = max(hit.char_start, span[0])
    hi = min(hit.char_end, span[1])
    if hi <= lo:
        return 0.0
    return (hi - lo) / max(1, span[1] - span[0])


def _first_rank(hits: Sequence[ChunkHit], span: Sequence[int]) -> Optional[int]:
    for i, h in enumerate(hits, start=1):
        if _contained(h, span):
            return i
    return None


# ==================== 单条 ====================


async def _run_one(
    case: Dict[str, Any],
    *,
    top_k: int,
    min_score: float,
    collection: str,
    embed_service: Any = None,
) -> Dict[str, Any]:
    span = case.get("answer_span")
    # 排序指标：不过阈值，看原始排序
    ranked = await search(
        case["query"],
        top_k=top_k,
        min_score=0.0,
        collection=collection,
        embed_service=embed_service,
    )
    # 拒答指标：用真实阈值
    gated = await search(
        case["query"],
        top_k=top_k,
        min_score=min_score,
        collection=collection,
        embed_service=embed_service,
    )

    out: Dict[str, Any] = {
        "id": case["id"],
        "kind": case["kind"],
        "query": case["query"],
        "lang_pair": case.get("lang_pair", "-"),
        "difficulty": case.get("difficulty", "-"),
        "repo": case.get("repo", ""),
        "best_score": ranked.best_score,
        "raw_hits": ranked.raw_hits,
        "reason": ranked.reason,
        "ranked_scores": [round(h.score, 4) for h in ranked.hits],
        "gated_hits": len(gated.hits),
    }

    if span is not None:
        rank = _first_rank(ranked.hits, span)
        out["first_rank"] = rank
        out["hit"] = {str(k): rank is not None and rank <= k for k in K_LIST}
        out["mrr"] = (1.0 / rank) if rank else 0.0
        out["top1_overlap"] = (
            round(_overlap_ratio(ranked.hits[0], span), 3) if ranked.hits else 0.0
        )
    else:
        out["false_accept"] = gated.answered
    return out


# ==================== 汇总 ====================


def _agg(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}
    scores = [r["best_score"] for r in rows]
    return {
        "n": n,
        "hit@1": sum(r["hit"]["1"] for r in rows) / n,
        "hit@3": sum(r["hit"]["3"] for r in rows) / n,
        "hit@5": sum(r["hit"]["5"] for r in rows) / n,
        "hit@10": sum(r["hit"]["10"] for r in rows) / n,
        "mrr": sum(r["mrr"] for r in rows) / n,
        "best_score_median": statistics.median(scores),
        "best_score_min": min(scores),
    }


def _pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def _table(rows: List[Dict[str, Any]], title: str) -> List[str]:
    lines = [f"### {title}\n", "| 分组 | n | hit@1 | hit@3 | hit@5 | hit@10 | MRR | 最高分中位 |", "|---|---|---|---|---|---|---|---|"]
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(r["lang_pair"], []).append(r)
    allagg = _agg(rows)
    lines.append(
        f"| **合计** | {allagg['n']} | {_pct(allagg['hit@1'])} | {_pct(allagg['hit@3'])} | "
        f"{_pct(allagg['hit@5'])} | {_pct(allagg['hit@10'])} | {allagg['mrr']:.3f} | "
        f"{allagg['best_score_median']:.4f} |"
    )
    for k in sorted(groups):
        a = _agg(groups[k])
        lines.append(
            f"| {k} | {a['n']} | {_pct(a['hit@1'])} | {_pct(a['hit@3'])} | "
            f"{_pct(a['hit@5'])} | {_pct(a['hit@10'])} | {a['mrr']:.3f} | "
            f"{a['best_score_median']:.4f} |"
        )
    lines.append("")
    return lines


def build_report(
    positives: List[Dict[str, Any]],
    negatives: List[Dict[str, Any]],
    *,
    tag: str,
    collection: str,
    top_k: int,
    min_score: float,
    elapsed_s: float,
    tokens: int,
    est_tokens: int,
    n_queries: int,
    meta: Dict[str, Any],
) -> str:
    L: List[str] = []
    L.append(f"# 检索评测报告 · {tag}\n")
    L.append(f"- 集合：`{collection}`")
    L.append(f"- 评测集：正样本 {len(positives)} 条 / 负样本 {len(negatives)} 条")
    L.append(f"- top_k = {top_k}（排序指标不过阈值）；拒答阈值 = {min_score}")
    L.append(f"- 耗时 {elapsed_s:.1f}s（{n_queries} 次查询）")
    if tokens:
        L.append(f"- embedding token：{tokens:,}（API 上报）")
    else:
        L.append(
            f"- embedding token：约 {est_tokens:,}（**字符数估算** —— 单条 embedding "
            f"接口不回填 usage，`total_tokens` 恒为 0）"
        )
    L.append(f"- 标签性质：**{meta.get('method', '?')}**（非独立人工标注）")
    L.append("")

    L.append("> ⚠️ **这个命中率是乐观上界**，不是绝对水平。理由：出题者读过原文，"
             "问题可能无意识地贴近原文用词。它可信的部分是**策略之间的相对高低**。\n")

    if positives:
        L.extend(_table(positives, "按语种对分组"))

        by_diff: Dict[str, List[Dict[str, Any]]] = {}
        for r in positives:
            by_diff.setdefault(r["difficulty"], []).append(r)
        L.append("### 按问题类型分组\n")
        L.append("| 类型 | n | hit@1 | hit@3 | hit@5 | MRR |")
        L.append("|---|---|---|---|---|---|")
        for k in sorted(by_diff):
            a = _agg(by_diff[k])
            L.append(
                f"| {k} | {a['n']} | {_pct(a['hit@1'])} | {_pct(a['hit@3'])} | "
                f"{_pct(a['hit@5'])} | {a['mrr']:.3f} |"
            )
        L.append("")

        misses = [r for r in positives if not r["hit"]["5"]]
        L.append(f"### 未命中（hit@5 失败，{len(misses)} 条）\n")
        if misses:
            L.append("| id | 问题 | 语种对 | 最高分 | 排名 | 前 3 名分数 |")
            L.append("|---|---|---|---|---|---|")
            for r in misses:
                L.append(
                    f"| {r['id']} | {r['query']} | {r['lang_pair']} | "
                    f"{r['best_score']:.4f} | {r['first_rank'] or '未进前 10'} | "
                    f"{r['ranked_scores'][:3]} |"
                )
        else:
            L.append("（无）")
        L.append("")

    if negatives:
        fa = [r for r in negatives if r["false_accept"]]
        scores = [r["best_score"] for r in negatives]
        L.append("### 负样本（语料外的问题，正确行为是拒答）\n")
        L.append(f"- 误纳率（best_score ≥ {min_score}）：**{len(fa)}/{len(negatives)}**")
        L.append(f"- 最高分：中位 {statistics.median(scores):.4f} / 最大 {max(scores):.4f}")
        L.append("")
        L.append("| id | 问题 | best_score | 是否误纳 |")
        L.append("|---|---|---|---|")
        for r in negatives:
            L.append(
                f"| {r['id']} | {r['query']} | {r['best_score']:.4f} | "
                f"{'❌ 误纳' if r['false_accept'] else '✅ 拒答'} |"
            )
        L.append("")

    return "\n".join(L)


# ==================== 主流程 ====================


# ==================== 策略对比（离线，不调 API） ====================


def _binom_two_sided(wins: int, losses: int) -> float:
    """符号检验（精确二项）：只有「一边命中、另一边没命中」的样本带信息。

    用符号检验而不是比总命中率，是因为总命中率把「两边都命中」的样本也算进去，
    会稀释真实差异。这里只看 discordant 对，问的是「翻转的方向是不是随机的」。
    """
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def build_compare(base: Dict[str, Any], chal: Dict[str, Any], k: int = 5) -> str:
    """两张基线并排比。`base` 是基线，`chal` 是挑战者。"""
    ta, tb = base.get("tag", "base"), chal.get("tag", "chal")
    sa, sb = base.get("summary", {}), chal.get("summary", {})
    la, lb = base.get("labeling_method"), chal.get("labeling_method")

    out: List[str] = []
    out.append(f"# 检索策略对比 · {ta}（基线） vs {tb}（挑战者）\n")
    out.append(f"- 基线：`{base.get('collection')}` / 生成于 {base.get('created')}")
    out.append(f"- 挑战者：`{chal.get('collection')}` / 生成于 {chal.get('created')}")
    out.append(f"- 样本：{sa.get('n')} 条正样本（两侧必须同集合，否则不可比）")
    out.append(f"- 判定口径：严格包含 / hit@{k} 用于符号检验\n")

    if la != lb or not la:
        out.append(
            f"> ⚠️ 两侧标注方式不一致（{la} vs {lb}）或缺失 —— 对比结论不可用。\n"
        )
    if sa.get("n") != sb.get("n"):
        out.append(
            f"> ⚠️ 样本数不同（{sa.get('n')} vs {sb.get('n')}）—— 总命中率不可直接比，"
            "下面只对**两侧都出现的 case** 做配对比较。\n"
        )

    # ---- 总体指标 ----
    out.append("## 一、总体指标\n")
    out.append(f"| 指标 | {ta} | {tb} | Δ（挑战者−基线） |")
    out.append("|---|---|---|---|")
    for key, label, fmt in (
        ("hit@1", "hit@1", _pct),
        ("hit@3", "hit@3", _pct),
        ("hit@5", "hit@5", _pct),
        ("hit@10", "hit@10", _pct),
    ):
        va, vb = sa.get(key), sb.get(key)
        if va is None or vb is None:
            continue
        d = vb - va
        arrow = "▲" if d > 0 else ("▼" if d < 0 else "＝")
        out.append(f"| {label} | {fmt(va)} | {fmt(vb)} | {arrow} {d * 100:+.1f}pt |")
    if sa.get("mrr") is not None and sb.get("mrr") is not None:
        d = sb["mrr"] - sa["mrr"]
        arrow = "▲" if d > 0 else ("▼" if d < 0 else "＝")
        out.append(f"| MRR | {sa['mrr']:.3f} | {sb['mrr']:.3f} | {arrow} {d:+.3f} |")
    fa_a, fa_b = base.get("false_accept_rate"), chal.get("false_accept_rate")
    if fa_a is not None and fa_b is not None:
        out.append(
            f"| 负样本误纳率 | {_pct(fa_a)} | {_pct(fa_b)} | "
            f"{'▲' if fa_b > fa_a else ('▼' if fa_b < fa_a else '＝')} "
            f"{(fa_b - fa_a) * 100:+.1f}pt |"
        )
    out.append("")

    # ---- 配对翻转（核心） ----
    rows_a = {r["id"]: r for r in base.get("positives", [])}
    rows_b = {r["id"]: r for r in chal.get("positives", [])}
    shared = [i for i in rows_a if i in rows_b]
    key = str(k)
    only_b, only_a, both = [], [], 0
    rank_gain, rank_loss = [], []
    for cid in shared:
        ha = bool(rows_a[cid].get("hit", {}).get(key))
        hb = bool(rows_b[cid].get("hit", {}).get(key))
        if hb and not ha:
            only_b.append(cid)
        elif ha and not hb:
            only_a.append(cid)
        else:
            both += 1
        ra, rb = rows_a[cid].get("first_rank"), rows_b[cid].get("first_rank")
        if ra and rb and ra != rb:
            (rank_gain if rb < ra else rank_loss).append((cid, ra, rb))

    wins, losses = len(only_b), len(only_a)
    p = _binom_two_sided(wins, losses)
    out.append(f"## 二、配对翻转（只看 hit@{k}）\n")
    out.append(f"- 两侧都命中：**{both}** 条（无信息量，不计入检验）")
    out.append(f"- **挑战者命中、基线未命中：{wins}** 条")
    out.append(f"- **基线命中、挑战者未命中：{losses}** 条")
    out.append(f"- 符号检验（精确二项，双侧）：**p = {p:.4f}**")
    out.append("")
    if wins + losses == 0:
        out.append(f"> 两侧在 hit@{k} 上**完全一致** —— 该指标分辨不出这两个策略。\n")
    elif p < 0.05:
        better = tb if wins > losses else ta
        out.append(f"> ✅ **显著差异（p < 0.05）**：**{better}** 明显更好。\n")
    else:
        out.append(
            f"> ⚠️ **差异不显著（p = {p:.4f} ≥ 0.05）**：{wins} 胜 {losses} 负，"
            "翻转方向还没超出随机波动。**不能据此宣布谁更好** —— 样本量（"
            f"{len(shared)} 条）不够，或两个策略确实接近。\n"
        )

    if only_b:
        out.append(f"**挑战者新增命中（{wins} 条）**\n")
        out.append(f"| id | 问题 | 基线排名 | 挑战者排名 |")
        out.append("|---|---|---|---|")
        for cid in only_b:
            out.append(
                f"| {cid} | {rows_b[cid]['query'][:40]} | "
                f"{rows_a[cid].get('first_rank') or '未进前 10'} | "
                f"{rows_b[cid].get('first_rank') or '未进前 10'} |"
            )
        out.append("")
    if only_a:
        out.append(f"**基线命中、挑战者丢失（{losses} 条）**\n")
        out.append(f"| id | 问题 | 基线排名 | 挑战者排名 |")
        out.append("|---|---|---|---|")
        for cid in only_a:
            out.append(
                f"| {cid} | {rows_a[cid]['query'][:40]} | "
                f"{rows_a[cid].get('first_rank') or '未进前 10'} | "
                f"{rows_b[cid].get('first_rank') or '未进前 10'} |"
            )
        out.append("")

    # ---- 分语种对 ----
    pairs = sorted({rows_a[c].get("lang_pair", "-") for c in shared})
    if pairs:
        out.append("## 三、分语种对（同一批 case 配对）\n")
        out.append(
            f"| 语种对 | n | {ta} hit@{k} | {tb} hit@{k} | Δ |"
        )
        out.append("|---|---|---|---|---|")
        for lp in pairs:
            ids = [c for c in shared if rows_a[c].get("lang_pair", "-") == lp]
            n = len(ids)
            ha = sum(bool(rows_a[c].get("hit", {}).get(key)) for c in ids) / n
            hb = sum(bool(rows_b[c].get("hit", {}).get(key)) for c in ids) / n
            d = hb - ha
            out.append(
                f"| {lp} | {n} | {_pct(ha)} | {_pct(hb)} | "
                f"{'▲' if d > 0 else ('▼' if d < 0 else '＝')} {d * 100:+.1f}pt |"
            )
        out.append("")

    # ---- 排名挪动（命中集合不变但顺序变了） ----
    if rank_gain or rank_loss:
        out.append("## 四、排名挪动（两侧都命中，但名次变了）\n")
        out.append(f"- 名次上升：{len(rank_gain)} 条；名次下降：{len(rank_loss)} 条")
        out.append("")
        out.append(f"| id | {ta} 排名 | {tb} 排名 | 变化 |")
        out.append("|---|---|---|---|")
        for cid, ra, rb in sorted(
            rank_gain + rank_loss, key=lambda x: x[1] - x[2]
        ):
            out.append(
                f"| {cid} | {ra} | {rb} | {'▲ 上升' if rb < ra else '▼ 下降'} |"
            )
        out.append("")

    # ---- 召回层 vs 排序层：把「切分」和「排序」两个问题分开 ----
    # 【为什么要单独拆这一节】hit@k 把两件事混在一起：答案有没有被召回到（召回层），
    # 和召回后排在几名（排序层）。如果两侧召回层打平，那差异就**全在排序上** ——
    # 这时候该动的是重排，不是切分策略。不拆开看，就会得出「该换切分器」的错误结论。
    out.append("## 五、召回层 vs 排序层（**这一节决定下一步该动哪里**）\n")
    found_a = [c for c in shared if rows_a[c].get("first_rank")]
    found_b = [c for c in shared if rows_b[c].get("first_rank")]
    out.append(
        f"- **召回层**（答案进入前 10）：{ta} **{len(found_a)}/{len(shared)}** · "
        f"{tb} **{len(found_b)}/{len(shared)}**"
    )
    if len(found_a) == len(found_b):
        out.append(
            "  → **两侧召回层打平**。也就是说差异**不在「能不能找到」，只在「排得够不够靠前」**"
            " —— 下一步该动的是**重排**，不是换切分策略。"
        )
    out.append("")

    # 配对 t 检验：用名次幅度（RR），比符号检验多用了「差多远」这个信息
    diff = [rows_b[c]["mrr"] - rows_a[c]["mrr"] for c in shared]
    if len(diff) >= 2 and statistics.stdev(diff) > 0:
        n = len(diff)
        mean_d = sum(diff) / n
        sd_d = statistics.stdev(diff)
        t_stat = mean_d / (sd_d / math.sqrt(n))
        out.append(f"- **配对 MRR**：{ta} {sa.get('mrr', 0):.3f} · {tb} {sb.get('mrr', 0):.3f}")
        out.append(
            f"- **配对 t 检验**（逐条 RR 差值，n={n}）：meanΔ {mean_d:+.4f} · "
            f"sd {sd_d:.4f} · **t = {t_stat:+.2f}**"
        )
        out.append(
            f"  → |t| = {abs(t_stat):.2f} "
            + ("≥" if abs(t_stat) >= 2 else "<")
            + " 2，"
            + (
                "**在 5% 水平上显著**"
                if abs(t_stat) >= 2
                else "**不显著**（n≥30 时 t 分布已接近正态，用 |t|>2 粗略判断即可）"
            )
        )
        out.append("")

    both_found = [
        c
        for c in shared
        if rows_a[c].get("first_rank") and rows_b[c].get("first_rank")
    ]
    if both_found:
        better = sum(
            1
            for c in both_found
            if rows_b[c]["first_rank"] < rows_a[c]["first_rank"]
        )
        worse = sum(
            1
            for c in both_found
            if rows_b[c]["first_rank"] > rows_a[c]["first_rank"]
        )
        out.append(
            f"- 两侧都召回到的 {len(both_found)} 条里，名次变化："
            f"{tb} 更好 **{better}** 条 / 更差 **{worse}** 条 / "
            f"不变 {len(both_found) - better - worse} 条"
        )
        out.append("")

    out.append("---\n")
    out.append(
        f"> ⚠️ 两侧都用**同一套 agent-grounded 标签**，所以出题者的用词偏好被抵消 —— "
        "**这个对比是可信的**。但它证明的是**相对高低**，不是任一侧的绝对水平。"
    )
    return "\n".join(out) + "\n"

def run_compare(args) -> int:
    pa = _HERE / f"retrieval_baseline_{args.compare[0]}.json"
    pb = _HERE / f"retrieval_baseline_{args.compare[1]}.json"
    for p in (pa, pb):
        if not p.exists():
            print(f"✗ 找不到基线文件: {p}")
            print("  先跑：retrieval_runner.py --collection <集合> --tag <tag>")
            return 1
    base = json.loads(pa.read_text(encoding="utf-8"))
    chal = json.loads(pb.read_text(encoding="utf-8"))
    report = build_compare(base, chal, k=args.compare_k)
    out_md = _HERE / f"retrieval_compare_{args.compare[0]}_vs_{args.compare[1]}.md"
    out_md.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n✓ 对比报告: {out_md}")
    return 0


async def run(args) -> int:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    negatives = data.get("negatives", [])
    if not cases and not negatives:
        print(f"✗ 评测集为空: {CASES_PATH}")
        return 1

    print("=" * 64)
    print(f"Retrieval Runner - 集合 {args.collection} / tag {args.tag}")
    print("=" * 64)
    print(f"正样本 {len(cases)} 条 / 负样本 {len(negatives)} 条")
    print(f"top_k={args.top_k}（排序口径）  阈值={args.min_score}（拒答口径）\n")

    from src.modules.agent.embedding_service import get_embedding_service
    from src.modules.rag.retriever import _embedding_api_key

    service = get_embedding_service(_embedding_api_key())
    try:
        service.reset_usage()
    except Exception:  # noqa: BLE001 - 没有这个接口也不影响评测
        pass

    started = time.monotonic()
    pos_rows: List[Dict[str, Any]] = []
    neg_rows: List[Dict[str, Any]] = []

    for i, case in enumerate(cases, start=1):
        row = await _run_one(
            case,
            top_k=args.top_k,
            min_score=args.min_score,
            collection=args.collection,
            embed_service=service,
        )
        pos_rows.append(row)
        mark = "✅" if row["hit"]["5"] else "❌"
        print(
            f"[{i:>2}/{len(cases)}] {mark} {row['id']} "
            f"rank={row['first_rank'] or '-':>2} best={row['best_score']:.4f}  {row['query'][:34]}"
        )

    for j, case in enumerate(negatives, start=1):
        row = await _run_one(
            case,
            top_k=args.top_k,
            min_score=args.min_score,
            collection=args.collection,
            embed_service=service,
        )
        neg_rows.append(row)
        mark = "❌误纳" if row["false_accept"] else "✅拒答"
        print(
            f"[N{j:>2}/{len(negatives)}] {mark} best={row['best_score']:.4f}  {row['query'][:34]}"
        )

    elapsed = time.monotonic() - started
    tokens = int(getattr(service, "total_tokens", 0) or 0)
    # 【为什么还要自己估一遍】单条 `generate_embedding` 走的是单文本协议，那条路径
    # 不一定回填 `usage`（批量路径才会）。所以 `total_tokens` 常常是 0 —— 直接显示
    # 会让人误以为「检索不花钱」。这里补一个字符数估算，并**标明是估算**。
    from src.modules.rag.chunk_spec import estimate_tokens

    est_tokens = sum(estimate_tokens(c["query"]) for c in cases + negatives)

    report = build_report(
        pos_rows,
        neg_rows,
        tag=args.tag,
        collection=args.collection,
        top_k=args.top_k,
        min_score=args.min_score,
        elapsed_s=elapsed,
        tokens=tokens,
        est_tokens=est_tokens,
        n_queries=len(cases) + len(negatives),
        meta=data.get("labeling", {}),
    )

    out_md = _HERE / f"retrieval_report_{args.tag}.md"
    out_json = _HERE / f"retrieval_baseline_{args.tag}.json"
    out_md.write_text(report, encoding="utf-8")

    agg = _agg(pos_rows) if pos_rows else {}
    baseline = {
        "tag": args.tag,
        "collection": args.collection,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "top_k": args.top_k,
        "min_score": args.min_score,
        "labeling_method": data.get("labeling", {}).get("method"),
        "summary": agg,
        "false_accept_rate": (
            sum(r["false_accept"] for r in neg_rows) / len(neg_rows)
            if neg_rows
            else None
        ),
        "negatives_n": len(neg_rows),
        "elapsed_s": round(elapsed, 1),
        "embedding_tokens": tokens,
        "positives": pos_rows,
        "negatives": neg_rows,
    }
    out_json.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n" + "=" * 64)
    if agg:
        print(
            f"命中率: hit@1 {_pct(agg['hit@1'])} | hit@3 {_pct(agg['hit@3'])} | "
            f"hit@5 {_pct(agg['hit@5'])} | hit@10 {_pct(agg['hit@10'])} | MRR {agg['mrr']:.3f}"
        )
    if neg_rows:
        fa = sum(r["false_accept"] for r in neg_rows)
        print(f"负样本误纳: {fa}/{len(neg_rows)}")
    print(f"耗时 {elapsed:.1f}s / token {tokens:,}")
    print(f"\n✓ 报告: {out_md}")
    print(f"✓ 基线: {out_json}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="检索评测执行器（hit@k / MRR / 拒答）+ 策略对比",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  跑一套策略：  python retrieval_runner.py --collection card_chunks --tag heading\n"
            "  对比两套策略：python retrieval_runner.py --compare heading fixed800\n"
        ),
    )
    p.add_argument("--collection", default="card_chunks", help="查哪个向量集合")
    p.add_argument("--tag", default="heading", help="报告文件后缀，用于区分不同策略")
    p.add_argument("--top-k", type=int, default=max(K_LIST), help="取几条（排序口径）")
    p.add_argument(
        "--min-score", type=float, default=DEFAULT_MIN_SCORE, help="拒答阈值（拒答口径）"
    )
    p.add_argument(
        "--compare",
        nargs=2,
        metavar=("基线TAG", "挑战者TAG"),
        help="离线对比两份基线（读 retrieval_baseline_<tag>.json，不调 API）",
    )
    p.add_argument(
        "--compare-k", type=int, default=5, help="对比时用哪个 k 做符号检验（默认 5）"
    )
    args = p.parse_args()
    if args.compare:
        return run_compare(args)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
