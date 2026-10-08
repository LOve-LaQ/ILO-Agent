# Tests - 注入前/后对照实验（统计口径与判分口径）
"""覆盖 `rag_chat_ab.py` 里「算错了也会出报告」的那几处。

## 为什么这些测试值得写

这个脚本本身不产出功能，它产出的是**结论**。而结论代码的错误会伪装成实验发现：

- `_sign_test_p` 用正态近似而不是精确二项 → 小样本上 p 值系统性偏小，
  会把「没效果」报成「有效果」；
- 组内标准差算错 → 噪声地板被低估 → 把生成抖动当成真实提升；
- `_group_stats` 把「未注入子集」混进「注入子集」→ 安慰剂失效，
  效果被稀释，且再也分不清「资料带来的」和「裁判漂移」；
- 判分 prompt 里泄露了组别 → 裁判的期望偏差会直接变成实验结论。

所以这里钉的是**口径**，不是功能。全部离线，不调任何模型。
"""

from __future__ import annotations

import asyncio

import pytest

import rag_chat_ab as AB


def _case_result(
    case_id: str = "c001",
    injected: bool = True,
    off: list | None = None,
    on: list | None = None,
) -> AB.CaseResult:
    """造一条结果。`off`/`on` 传的是每轮的分数字典列表。"""
    r = AB.CaseResult(
        case_id=case_id,
        query="问题",
        repo="owner/repo",
        injected=injected,
        reason="ok" if injected else "below_threshold",
        refs=3 if injected else 0,
    )
    r.scores_off = list(off or [])
    r.scores_on = list(on or [])
    return r


def _scores(*totals: int) -> list:
    """把每个期望总分展开成合法的分数字典（每维 1~5，三维合计 3~15）。

    三个维度各自都有下限 1，所以**总分不能直接塞进 correctness** ——
    那样总分会比预期多 2。这里按维度从低到高填满，保证 `sum(字典) == 期望总分`。
    """
    out = []
    for t in totals:
        t = max(3, min(5 * len(AB.JUDGE_DIMENSIONS), t))
        values = [1] * len(AB.JUDGE_DIMENSIONS)
        rest = t - len(values)
        for i in range(len(values)):
            add = min(5 - values[i], rest)
            values[i] += add
            rest -= add
        out.append(dict(zip(AB.JUDGE_DIMENSIONS, values)))
    return out


# --------------------------------------------------------------------------- #
# 一、符号检验：必须用精确二项，不能用正态近似
# --------------------------------------------------------------------------- #


def test_sign_test_no_discordant_pairs_is_one():
    """没有一胜一负的配对时，p 值定义为 1（不显著）。"""
    assert AB._sign_test_p(0, 0) == 1.0


def test_sign_test_exact_values():
    """精确双侧 p 值：n=5 时 5:0 → 0.0625、4:1 → 0.375。"""
    assert AB._sign_test_p(5, 0) == 0.0625
    assert AB._sign_test_p(4, 1) == 0.375


def test_sign_test_is_symmetric():
    """符号检验只看「方向对不对」，不看方向朝哪 —— 交换胜负 p 值必须相同。"""
    assert AB._sign_test_p(5, 0) == AB._sign_test_p(0, 5)
    assert AB._sign_test_p(7, 2) == AB._sign_test_p(2, 7)


def test_sign_test_caps_at_one():
    """极端均衡时精确双侧 p 会算到 >1，必须截到 1 —— 不能出现 p=1.31 这种值。"""
    assert AB._sign_test_p(3, 3) == 1.0
    assert AB._sign_test_p(1, 1) == 1.0


def test_sign_test_is_conservative_than_normal_approx_on_small_n():
    """小样本上精确检验必须比正态近似更保守（p 更大）——这是它存在的理由。

    正态近似算 5:0 的 z = 5/sqrt(5) = 2.236，单侧 p≈0.0127、双侧≈0.025，
    比精确的 0.0625 小得多。用近似就会把「5 比 0」误报成显著。
    """
    exact = AB._sign_test_p(5, 0)
    normal_approx = 0.025
    assert exact > normal_approx


# --------------------------------------------------------------------------- #
# 二、逐条汇总：均值、组内标准差（噪声地板）
# --------------------------------------------------------------------------- #


def test_case_result_means_over_repeats():
    """多次重复时，总分取**均值**而不是最后一次 —— 否则 --repeat 就是摆设。"""
    r = _case_result(off=_scores(3, 6, 9), on=_scores(6, 6, 6))
    assert r.total_off == 6.0
    assert r.total_on == 6.0


def test_case_result_std_is_none_when_single_round():
    """只跑一轮时没有组内标准差 —— 必须返回 None，让报告能显式提示「无法估计噪声」。"""
    r = _case_result(off=_scores(5), on=_scores(9))
    assert r.std_off is None
    assert r.std_on is None


def test_case_result_std_is_population_std():
    """组内标准差用总体标准差（这一组重复就是全部样本，不是抽样）。"""
    r = _case_result(off=_scores(3, 6, 9), on=_scores(6, 6, 6))
    assert r.std_off == pytest.approx(2.449, abs=1e-3)  # pstdev(3,6,9) = sqrt(6)
    assert r.std_on == 0.0


def test_case_result_handles_no_scores():
    """一条分数都没有（全判分失败）时总分为 None，不能被当成 0 分参与均值。"""
    r = _case_result(off=[], on=[])
    assert r.total_off is None
    assert r.total_on is None


# --------------------------------------------------------------------------- #
# 三、分组统计：注入子集 / 安慰剂子集必须分开
# --------------------------------------------------------------------------- #


def test_group_stats_separates_injected_and_placebo():
    """注入子集和未注入子集必须能分别统计 —— 安慰剂是整轮结论的对照组。"""
    injected = _case_result("c001", injected=True, off=_scores(3), on=_scores(9))
    placebo = _case_result("c002", injected=False, off=_scores(6), on=_scores(6))

    g_inj = AB._group_stats([injected], "注入")
    g_pla = AB._group_stats([placebo], "安慰剂")

    assert g_inj["n"] == 1 and g_inj["wins"] == 1 and g_inj["losses"] == 0
    assert g_pla["n"] == 1 and g_pla["wins"] == 0 and g_pla["losses"] == 0
    assert g_pla["ties"] == 1


def test_group_stats_counts_wins_losses_ties():
    """逐条胜负按总分比 —— 平局单独计数，不能并进任何一边。"""
    results = [
        _case_result("c1", off=_scores(3), on=_scores(9)),   # B 胜
        _case_result("c2", off=_scores(9), on=_scores(3)),   # A 胜
        _case_result("c3", off=_scores(6), on=_scores(6)),   # 平
    ]
    g = AB._group_stats(results, "全量")
    assert (g["wins"], g["losses"], g["ties"]) == (1, 1, 1)


def test_group_stats_aggregates_noise_floor():
    """组内标准差要汇总成噪声地板 —— 这是判断「差值是不是真效果」的唯一依据。"""
    results = [
        _case_result("c1", off=_scores(3, 6, 9), on=_scores(6, 6, 6)),
        _case_result("c2", off=_scores(4, 6, 8), on=_scores(5, 5, 5)),
    ]
    g = AB._group_stats(results, "全量")
    # c1: pstdev(3,6,9) = 2.449 ｜ c2: pstdev(4,6,8) = 1.633 → 均值 2.041
    assert g["noise_off"] == 2.041
    assert g["noise_on"] == 0.0


def test_group_stats_noise_is_none_without_repeats():
    """k=1 时必须返回 None，而不是 0 —— 0 会被误读成「噪声为零、差值可信」。"""
    g = AB._group_stats([_case_result(off=_scores(3), on=_scores(9))], "全量")
    assert g["noise_off"] is None
    assert g["noise_on"] is None


def test_group_stats_empty_group_is_safe():
    """空分组（例如一条都没注入）不能崩，也不能除零。"""
    g = AB._group_stats([], "注入子集")
    assert g["n"] == 0
    assert g["total_off"] is None
    assert g["p_value"] is None


# --------------------------------------------------------------------------- #
# 四、判分 prompt：盲评是结论能站住的前提
# --------------------------------------------------------------------------- #


def test_judge_prompt_carries_question_gold_and_answer():
    """裁判必须同时看到问题、参考答案、被评回答 —— 少了参考答案就没法判正确性。"""
    p = AB._build_judge_prompt("问题X", "参考答案Y", "被评回答Z")
    assert "问题X" in p
    assert "参考答案Y" in p
    assert "被评回答Z" in p


def test_judge_prompt_is_blind_to_the_arm():
    """判分 prompt 里**不能**出现「A 组 / B 组 / 有资料 / 检索」这类字眼。

    裁判一旦知道哪份是有资料辅助的，就会不自觉地给高分（期望偏差），
    那这个实验测的就是裁判的偏好，不是模型的能力。
    """
    p = AB._build_judge_prompt("问题X", "参考答案Y", "被评回答Z")
    for leak in ("A 组", "B 组", "关检索", "开检索", "参考资料", "检索结果"):
        assert leak not in p


def test_judge_prompt_lists_all_dimensions():
    """三个维度必须都出现在 prompt 里，否则模型会漏答某一维、被记成 judge_error。"""
    p = AB._build_judge_prompt("q", "g", "a")
    for dim in AB.JUDGE_DIMENSIONS:
        assert dim in p


# --------------------------------------------------------------------------- #
# 五、判分输出解析
# --------------------------------------------------------------------------- #


def test_parse_judge_json_plain():
    data = AB._parse_judge_json('{"correctness": 4, "reason": "还行"}')
    assert data["correctness"] == 4


def test_parse_judge_json_tolerates_code_fence():
    """裁判偶尔会用 ```json 包裹 —— 容忍它，不要为此丢掉一条样本。"""
    raw = '```json\n{"correctness": 5}\n```'
    assert AB._parse_judge_json(raw)["correctness"] == 5


def test_parse_judge_json_rejects_garbage():
    """完全不是 JSON 时必须返回 None（由调用方记 judge_error 重试）。"""
    assert AB._parse_judge_json("我觉得这个回答挺好的") is None
    assert AB._parse_judge_json("") is None


# --------------------------------------------------------------------------- #
# 六、会话上下文构造
# --------------------------------------------------------------------------- #


def test_session_for_uses_repo_as_topic():
    """主题取仓库名（用户真实看到的卡片标题）。"""
    s = AB._session_for({"repo": "owner/repo", "id": "c001"})
    assert s["topic"] == "owner/repo"


def test_session_for_leaves_summary_and_concepts_empty():
    """摘要与核心概念**故意留空**：给了它们，A 组会沾光，效果被稀释。"""
    s = AB._session_for({"repo": "owner/repo"})
    assert s["summary"] == ""
    assert s["core_concepts"] == []


# --------------------------------------------------------------------------- #
# 七、报告：必须带上「不可下结论」的提示
# --------------------------------------------------------------------------- #


def test_render_warns_when_repeat_is_one():
    """repeat=1 时报告必须显式警告 —— 否则一张「两组差不多」的表会被当成结论。"""
    report = AB._render([_case_result(off=_scores(3), on=_scores(9))], True, repeat=1)
    assert "repeat=1" in report
    assert "不要用来下结论" in report


def test_render_labels_placebo_group():
    """未注入子集必须被明确标成安慰剂（提示词逐字相同 → 差值就是噪声）。"""
    report = AB._render(
        [_case_result("c001", injected=False, off=_scores(6), on=_scores(6))],
        True,
        repeat=3,
    )
    assert "安慰剂" in report


def test_render_without_judge_skips_score_tables():
    """--no-judge 时只出逐条明细，不该出现分数表（没有分数可报）。"""
    report = AB._render([_case_result(off=[], on=[])], do_judge=False, repeat=1)
    assert "逐条明细" in report
    assert "配对符号检验" not in report


# --------------------------------------------------------------------------- #
# 八、预检：Qdrant 不可用必须**失败退出**，不能静默降级成「实验结论」
# --------------------------------------------------------------------------- #


def test_preflight_flags_qdrant_error(monkeypatch):
    """检索返回 qdrant_error 时，预检必须报错 —— 否则 36 条题会全部空转，
    两组提示词逐字相同，跑出一张「注入没效果」的假表。"""

    class _Outcome:
        reason = "qdrant_error"

    async def _fake(message, **kwargs):
        return _Outcome()

    monkeypatch.setattr(AB, "build_chat_context", _fake)
    problem = asyncio.run(AB._preflight())
    assert problem is not None
    assert "qdrant" in problem.lower()
    assert "docker compose up -d qdrant" in problem


def test_preflight_passes_when_retrieval_works(monkeypatch):
    """检索正常（哪怕没命中）时预检必须放行 —— below_threshold 是正常结果，不是故障。"""

    class _Outcome:
        reason = "below_threshold"

    async def _fake(message, **kwargs):
        return _Outcome()

    monkeypatch.setattr(AB, "build_chat_context", _fake)
    assert asyncio.run(AB._preflight()) is None


# --------------------------------------------------------------------------- #
# 九、判分依据：必须回原文取窗口，不能直接用 answer_excerpt
# --------------------------------------------------------------------------- #


def test_gold_reference_window_zero_falls_back_to_excerpt(monkeypatch):
    """`--gold-window 0` 时退回 answer_excerpt，且**不该去查库**。"""

    def _boom(*a, **k):
        raise AssertionError("half_chars=0 时不应调用 hydrate_texts")

    monkeypatch.setattr(AB, "hydrate_texts", _boom)
    case = {"answer_excerpt": "锚点句", "answer_span": [100, 150], "card_id": "gh-x"}
    assert AB._gold_reference(case, 0) == "锚点句"


def test_gold_reference_without_span_falls_back_to_excerpt(monkeypatch):
    """没有 answer_span 的用例退回 excerpt，不能崩。"""
    monkeypatch.setattr(AB, "hydrate_texts", lambda *a, **k: [])
    case = {"answer_excerpt": "锚点句", "card_id": "gh-x"}
    assert AB._gold_reference(case, 600) == "锚点句"


def test_gold_reference_returns_window_around_span(monkeypatch):
    """正常路径：按答案区间前后各取 half 字符回库取正文。"""
    seen = {}

    class _Chunk:
        text = "窗口正文"

    def _fake(hits, **kwargs):
        seen["start"] = hits[0].char_start
        seen["end"] = hits[0].char_end
        seen["card"] = hits[0].card_id
        return [_Chunk()]

    monkeypatch.setattr(AB, "hydrate_texts", _fake)
    case = {"answer_excerpt": "锚点句", "answer_span": [1000, 1050], "card_id": "gh-x"}

    assert AB._gold_reference(case, 600) == "窗口正文"
    assert seen == {"start": 400, "end": 1650, "card": "gh-x"}


def test_gold_reference_clamps_start_at_zero(monkeypatch):
    """答案区间靠近开头时，起点必须夹到 0 —— 负数区间取不到正文，会静默退回弱依据。"""
    seen = {}

    class _Chunk:
        text = "窗口正文"

    def _fake(hits, **kwargs):
        seen["start"] = hits[0].char_start
        return [_Chunk()]

    monkeypatch.setattr(AB, "hydrate_texts", _fake)
    case = {"answer_excerpt": "锚点句", "answer_span": [100, 150], "card_id": "gh-x"}

    AB._gold_reference(case, 600)
    assert seen["start"] == 0


def test_gold_reference_falls_back_when_hydration_fails(monkeypatch):
    """阶梯全部失败（原文变短 / 越界 / 库中无此卡）时退回 excerpt —— 宁可依据弱，也不要整条样本作废。"""

    class _Chunk:
        text = None

    monkeypatch.setattr(AB, "hydrate_texts", lambda *a, **k: [_Chunk()])
    case = {"answer_excerpt": "锚点句", "answer_span": [100, 150], "card_id": "gh-x"}
    assert AB._gold_reference(case, 600) == "锚点句"


def test_shrink_ladder_ends_at_zero():
    """阶梯必须以 0 收尾 —— 0 表示「窗口 = 答案区间」，而答案区间必然落在原文内，
    所以最后一级一定能取到正文，函数有确定的收敛保证。"""
    assert AB._shrink_ladder(600) == [600, 300, 150, 75, 37, 18, 9, 4, 2, 1, 0]


def test_gold_reference_shrinks_window_until_it_fits(monkeypatch):
    """窗口越界时**逐级收窄**，而不是一次失败就退回窄锚点。

    实测有 2/36 条题的答案就在文末，`+600` 会越过原文长度。如果一次失败就退回
    `answer_excerpt`，那恰好是在最需要宽窗口的样本上退回最弱的依据。
    """
    calls = []

    class _Chunk:
        def __init__(self, text):
            self.text = text

    def _fake(hits, **kwargs):
        end = hits[0].char_end
        calls.append(end)
        # 模拟「文档只有 1200 字符」：窗口越界就拒绝
        return [_Chunk(None if end > 1200 else "窗口正文")]

    monkeypatch.setattr(AB, "hydrate_texts", _fake)
    case = {"answer_excerpt": "锚点句", "answer_span": [1000, 1100], "card_id": "gh-x"}

    assert AB._gold_reference(case, 600) == "窗口正文"
    assert len(calls) > 1, "应当重试了更小的窗口"
    assert calls[-1] <= 1200, "最终成功的窗口必须落在文档内"


# --------------------------------------------------------------------------- #
# 十、报告解析（交叉分析的基础，解析错了交叉表就是错的）
# --------------------------------------------------------------------------- #

_SAMPLE_REPORT = """\
==============================================================================
逐条明细
==============================================================================
c001  [注入]  refs=5  reason=ok  AirCard-iOS 的 ipa 包可以用哪些工具侧载安装？
      A=9.67/15  B=15.00/15   B 胜  组内σ=0.94/0.00
c002  [未注入]  refs=0  reason=below_threshold  SmartCV 后端为什么用 8600 端口
      A=11.00/15  B=10.00/15   A 胜  组内σ=2.94/2.16
c003  [注入]  refs=3  reason=ok  某条没有分数的题
"""


def test_parse_report_extracts_rows():
    """能从报告里解出「每条题 A/B 各多少分、有没有注入」。"""
    rows = AB._parse_report(_SAMPLE_REPORT)
    assert rows == [
        ("c001", True, 9.67, 15.00),
        ("c002", False, 11.00, 10.00),
    ]


def test_parse_report_skips_rows_without_scores():
    """没有分数行的条目（例如只跑了 --no-judge）必须跳过，不能造出假分数。"""
    rows = AB._parse_report(_SAMPLE_REPORT)
    assert all(r[0] != "c003" for r in rows)


def test_parse_report_on_empty_text():
    assert AB._parse_report("") == []
