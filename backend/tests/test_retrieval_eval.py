# Tests - 检索评测（切分对照组 + 指标计算）
"""覆盖 P3 评测链路上「算错了也不报错」的那几处。

## 为什么这些测试值得写

评测代码本身不产出功能，但**它的错误会伪装成结论**：
- `chunk_fixed` 破坏区间不变式 → 评测集里锚定字符区间的标签和对照组对不上，
  对照组命中率会凭空变低，而报告上看不出任何异常；
- `_contained` 写成「重叠」而不是「包含」→ 命中率虚高，且虚高的部分恰好是
  「答案跨在两块交界、各拿一半」这种**模型拿不到完整信息**的情况；
- `build_report` 漏掉「标签是自动生成」的免责声明 → 那个数字会被当成
  真实用户满意度读。

所以这里钉的是「口径」而不是「功能」。
"""

from __future__ import annotations

import backfill_chunks_alt as ALT
import retrieval_runner as RUN

from src.modules.rag.retriever import ChunkHit


def _hit(start: int, end: int, score: float = 0.5, ordinal: int = 0) -> ChunkHit:
    return ChunkHit(
        card_id="gh-test",
        ordinal=ordinal,
        score=score,
        char_start=start,
        char_end=end,
    )


# ==================== chunk_fixed：区间不变式 ====================


def test_chunk_fixed_preserves_char_interval_invariant():
    """`text == 原文[char_start:char_end]` —— 对照组的标签能对上评测集的前提"""
    raw = ("# 标题\n\n" + "这是一段中文正文，用来测试切分。" * 400) + "\n\n## 小节\n" + "tail " * 200
    chunks = ALT.chunk_fixed(raw, size=300, overlap_ratio=0.15)
    assert chunks, "应当切出块"
    for ch in chunks:
        assert ch["text"] == raw[ch["char_start"] : ch["char_end"]], (
            f"区间不变式被破坏：ordinal={ch['ordinal']} "
            f"span=[{ch['char_start']},{ch['char_end']})"
        )


def test_chunk_fixed_ordinals_are_contiguous_from_zero():
    raw = "x" * 5000
    chunks = ALT.chunk_fixed(raw, size=400, overlap_ratio=0.15)
    assert [c["ordinal"] for c in chunks] == list(range(len(chunks)))


def test_chunk_fixed_covers_whole_text():
    """块并集要覆盖全文 —— 有空洞等于有内容永远检索不到"""
    raw = "".join(f"line {i}\n" for i in range(600))
    chunks = ALT.chunk_fixed(raw, size=300, overlap_ratio=0.15)
    assert chunks[0]["char_start"] == 0
    assert chunks[-1]["char_end"] == len(raw)
    for prev, nxt in zip(chunks, chunks[1:]):
        # 相邻块必须重叠（否则中间那段文字不属于任何块）
        assert nxt["char_start"] <= prev["char_end"]


def test_chunk_fixed_strictly_advances_and_terminates():
    """每轮起点必须前进 —— 否则遇到极短窗口会死循环"""
    raw = "y" * 10000
    chunks = ALT.chunk_fixed(raw, size=100, overlap_ratio=0.15)
    starts = [c["char_start"] for c in chunks]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts), "起点重复 = 死循环或重复块"
    assert len(chunks) < 1000, "块数异常，可能是没有前进"


def test_chunk_fixed_handles_empty_and_short():
    assert ALT.chunk_fixed("") == []
    assert ALT.chunk_fixed("   \n  ") == []
    one = ALT.chunk_fixed("短文本", size=800)
    assert len(one) == 1
    assert one[0]["char_start"] == 0 and one[0]["char_end"] == 3


def test_chunk_fixed_rejects_bad_params():
    import pytest

    with pytest.raises(ValueError):
        ALT.chunk_fixed("abc", size=0)
    with pytest.raises(ValueError):
        ALT.chunk_fixed("abc", size=100, overlap_ratio=1.0)
    with pytest.raises(ValueError):
        ALT.chunk_fixed("abc", size=100, overlap_ratio=-0.1)


def test_chunk_fixed_does_not_respect_headings():
    """对照组的意义就在于「不看结构」—— 标题不参与切分

    这条是**反向断言**：如果哪天有人给对照组加上了标题切分，它就失去了
    「对照组」的资格，这个测试会拦住。
    """
    raw = "## A\n" + "a" * 700 + "\n## B\n" + "b" * 700
    chunks = ALT.chunk_fixed(raw, size=800, overlap_ratio=0.0)
    # 800 的窗口跨过了 "## B"，说明没有在标题处断开
    assert any("## B" in c["text"] and "## A" in c["text"] for c in chunks)
    assert all(c["heading_path"] == [] for c in chunks)


# ==================== 指标：包含 vs 重叠 ====================


def test_contained_requires_full_coverage():
    """「包含」是严口径：块只覆盖一半不算命中"""
    span = [1000, 1200]
    assert RUN._contained(_hit(900, 1300), span) is True
    assert RUN._contained(_hit(1000, 1200), span) is True
    # 只覆盖前半 / 后半 —— 模型拿到的是残缺信息，不算命中
    assert RUN._contained(_hit(900, 1100), span) is False
    assert RUN._contained(_hit(1100, 1300), span) is False
    # 完全在区间内（比答案还小）也不算
    assert RUN._contained(_hit(1050, 1100), span) is False


def test_overlap_ratio_is_diagnostic_only():
    span = [1000, 1200]
    assert RUN._overlap_ratio(_hit(900, 1100), span) == 0.5
    assert RUN._overlap_ratio(_hit(1100, 1300), span) == 0.5
    assert RUN._overlap_ratio(_hit(0, 10), span) == 0.0


def test_first_rank_uses_containment_not_overlap():
    span = [1000, 1200]
    hits = [_hit(900, 1100), _hit(800, 1200), _hit(950, 1250)]
    # 第 1 条只重叠一半 → 跳过；第 2 条包含 → rank=2
    assert RUN._first_rank(hits, span) == 2
    assert RUN._first_rank([_hit(0, 10)], span) is None
    assert RUN._first_rank([], span) is None


def test_agg_hit_curve_is_monotonic():
    rows = [
        {"hit": {"1": True, "3": True, "5": True, "10": True}, "mrr": 1.0,
         "best_score": 0.7},
        {"hit": {"1": False, "3": True, "5": True, "10": True}, "mrr": 0.5,
         "best_score": 0.6},
        {"hit": {"1": False, "3": False, "5": True, "10": True}, "mrr": 0.2,
         "best_score": 0.5},
        {"hit": {"1": False, "3": False, "5": False, "10": False}, "mrr": 0.0,
         "best_score": 0.4},
    ]
    a = RUN._agg(rows)
    assert a["n"] == 4
    assert a["hit@1"] == 0.25
    assert a["hit@3"] == 0.5
    assert a["hit@5"] == 0.75
    # 第 4 条连前 10 都没进（真实的「检索没召回到」），所以 hit@10 也是 0.75
    assert a["hit@10"] == 0.75
    # hit@k 必须随 k 单调不减 —— 不满足说明判定逻辑写错了
    assert a["hit@1"] <= a["hit@3"] <= a["hit@5"] <= a["hit@10"]
    assert a["mrr"] == (1.0 + 0.5 + 0.2 + 0.0) / 4


def test_agg_on_empty_returns_zero_n():
    assert RUN._agg([]) == {"n": 0}


# ==================== 报告：免责声明必须在 ====================


def test_report_carries_labeling_caveat():
    """标签是自动生成的 —— 报告里必须写明，否则命中率会被当成绝对水平"""
    rows = [
        {
            "id": "c001", "kind": "positive", "query": "q", "lang_pair": "zh_query_en_doc",
            "difficulty": "detail", "repo": "r", "best_score": 0.6, "raw_hits": 5,
            "reason": "ok", "ranked_scores": [0.6], "gated_hits": 1,
            "first_rank": 1, "hit": {"1": True, "3": True, "5": True, "10": True},
            "mrr": 1.0, "top1_overlap": 1.0,
        }
    ]
    report = RUN.build_report(
        rows, [],
        tag="t", collection="c", top_k=10, min_score=0.35,
        elapsed_s=1.0, tokens=0, est_tokens=10, n_queries=1,
        meta={"method": "agent-grounded"},
    )
    assert "乐观上界" in report
    assert "agent-grounded" in report
    # 单条 embedding 不回填 usage，报告不能因此显示「0 token」而不加说明
    assert "估算" in report


def test_report_lists_false_accepts():
    negs = [
        {"id": "n001", "kind": "negative", "query": "天气", "lang_pair": "-",
         "difficulty": "-", "repo": "", "best_score": 0.9, "raw_hits": 5,
         "reason": "ok", "ranked_scores": [0.9], "gated_hits": 1, "false_accept": True},
    ]
    report = RUN.build_report(
        [], negs,
        tag="t", collection="c", top_k=10, min_score=0.35,
        elapsed_s=1.0, tokens=0, est_tokens=1, n_queries=1, meta={},
    )
    assert "误纳率" in report
    assert "1/1" in report


# ==================== 策略对比：符号检验 + 配对翻转 ====================


def _baseline(tag, hit5_flags, lang_pairs=None, hit1_flags=None):
    """造一份最小基线：hit5_flags 是每条 case 的 hit@5 布尔值。"""
    positives = []
    for i, h5 in enumerate(hit5_flags):
        h1 = (hit1_flags or hit5_flags)[i]
        positives.append({
            "id": f"c{i:03d}", "kind": "positive", "query": f"q{i}",
            "lang_pair": (lang_pairs or ["zh_query_zh_doc"] * len(hit5_flags))[i],
            "difficulty": "-", "repo": "", "best_score": 0.5, "raw_hits": 5,
            "reason": "ok", "ranked_scores": [0.5], "gated_hits": 1,
            "first_rank": 2 if h5 else None,
            "hit": {"1": h1, "3": h5, "5": h5, "10": h5},
            "mrr": 0.5 if h5 else 0.0,
        })
    return {
        "tag": tag, "collection": f"coll_{tag}", "created": "2026-10-08 00:00:00",
        "top_k": 10, "min_score": 0.35, "labeling_method": "agent-grounded",
        "summary": RUN._agg(positives), "false_accept_rate": 0.0,
        "negatives_n": 0, "positives": positives, "negatives": [],
    }


def test_binom_two_sided_extremes():
    # 全部一致 → 不显著
    assert RUN._binom_two_sided(0, 0) == 1.0
    # 10 胜 0 负 → 约 2 * (1/1024)，显著
    assert RUN._binom_two_sided(10, 0) < 0.01
    # 3 胜 3 负 → 完全对称，p = 1
    assert RUN._binom_two_sided(3, 3) == 1.0
    # 5 胜 0 负 → 2 * (1/32) = 0.0625，刚好不显著
    assert RUN._binom_two_sided(5, 0) > 0.05


def test_compare_detects_significant_challenger_win():
    # 基线 8 条全不中，挑战者 8 条全中 → 8 胜 0 负
    base = _baseline("base", [False] * 8)
    chal = _baseline("chal", [True] * 8)
    report = RUN.build_compare(base, chal, k=5)
    assert "显著差异" in report
    assert "挑战者命中、基线未命中：8" in report
    assert "基线命中、挑战者未命中：0" in report
    assert "chal" in report


def test_compare_refuses_to_crown_when_not_significant():
    # 3 胜 3 负 → 不显著，报告必须明说「不能据此宣布谁更好」
    base = _baseline("base", [True, True, True, False, False, False])
    chal = _baseline("chal", [False, False, False, True, True, True])
    report = RUN.build_compare(base, chal, k=5)
    assert "差异不显著" in report
    assert "不能据此宣布" in report


def test_compare_flags_identical_strategies():
    base = _baseline("base", [True, False, True])
    chal = _baseline("chal", [True, False, True])
    report = RUN.build_compare(base, chal, k=5)
    assert "完全一致" in report


def test_compare_carries_relative_only_caveat():
    base = _baseline("base", [True, False])
    chal = _baseline("chal", [False, True])
    report = RUN.build_compare(base, chal, k=5)
    # 相对结论可信、绝对水平不可信的免责声明必须在
    assert "相对高低" in report
    assert "agent-grounded" in report


def test_compare_warns_on_mismatched_labeling_method():
    base = _baseline("base", [True, False])
    chal = _baseline("chal", [True, False])
    chal["labeling_method"] = "human"
    report = RUN.build_compare(base, chal, k=5)
    assert "标注方式不一致" in report


def test_compare_groups_by_lang_pair():
    base = _baseline(
        "base", [False, False, True, True],
        lang_pairs=["zh_query_en_doc"] * 2 + ["zh_query_zh_doc"] * 2,
    )
    chal = _baseline(
        "chal", [True, True, True, True],
        lang_pairs=["zh_query_en_doc"] * 2 + ["zh_query_zh_doc"] * 2,
    )
    report = RUN.build_compare(base, chal, k=5)
    assert "zh_query_en_doc" in report
    assert "zh_query_zh_doc" in report


def test_compare_separates_recall_from_ranking():
    """两侧召回层打平时，必须点明「该动重排，不是换切分」。

    这是这一节存在的理由：hit@k 把「找没找到」和「排得靠不靠前」混在一起，
    不拆开就会得出「该换切分器」的错误结论。
    """
    # 两边都能召回（first_rank 都非空）→ 召回层打平
    base = _baseline("base", [True, True, False, False])
    chal = _baseline("chal", [False, False, True, True])
    report = RUN.build_compare(base, chal, k=5)
    assert "召回层" in report
    assert "打平" in report
    assert "重排" in report


def test_compare_reports_paired_t_statistic():
    base = _baseline("base", [False, False, True, True])
    chal = _baseline("chal", [True, True, True, True])
    report = RUN.build_compare(base, chal, k=5)
    assert "配对 t 检验" in report
    assert "t = " in report


def test_compare_detects_recall_layer_regression():
    """一侧召回层更差时，不能再说「打平」。"""
    base = _baseline("base", [True, True, True, True])
    chal = _baseline("chal", [True, False, False, False])
    report = RUN.build_compare(base, chal, k=5)
    assert "打平" not in report
