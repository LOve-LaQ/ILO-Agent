# Tests - Markdown 切分器
"""`src/modules/rag/chunker.py` 的离线单测。

全部用构造文本，不碰数据库 / Qdrant / LLM —— 切分规则本身必须能被单独验证，
否则一旦检索结果不对，无法判断是「切分错了」还是「向量或检索错了」。

最要紧的一条是**区间不变式**：每块的 `text` 必须等于原文按 `[char_start, char_end)`
切片的结果。任务 B 的评测要靠它把「答案所在的原文区间」和「检索命中的块」机械地
对上；这条一旦破了，跨切分策略的对比表就做不出来。
"""

import pytest

from src.modules.rag.chunker import _CONTENTLESS_RE, chunk_markdown

# ---------------------------------------------------------------- 工具


def _assert_span_invariant(text: str, chunks: list[dict]) -> None:
    """核心不变式：text == 原文[char_start:char_end]"""
    for chunk in chunks:
        assert chunk["text"] == text[chunk["char_start"] : chunk["char_end"]], (
            f"第 {chunk['ordinal']} 块的 text 与原文区间对不上："
            f"[{chunk['char_start']}, {chunk['char_end']})"
        )


def _assert_ordered_and_contiguous(chunks: list[dict]) -> None:
    """块的序号递增；区间单调不减（重叠时后块起点会前移，但不会越过前块终点）"""
    for index, chunk in enumerate(chunks):
        assert chunk["ordinal"] == index
        assert chunk["char_start"] < chunk["char_end"]
        if index:
            prev = chunks[index - 1]
            assert chunk["char_start"] <= prev["char_end"]


def _chunk(text: str, **kwargs) -> list[dict]:
    """测试默认关掉「过短合并」，让结构类断言不被合并行为干扰"""
    kwargs.setdefault("min_chars", 1)
    return chunk_markdown(text, **kwargs)


# ---------------------------------------------------------------- 入口边界


@pytest.mark.parametrize("text", ["", "   ", "\n\n\t "], ids=["empty", "spaces", "newlines"])
def test_blank_input_returns_no_chunks(text):
    assert chunk_markdown(text) == []


@pytest.mark.parametrize("kwargs", [{"max_chars": 0}, {"max_chars": -1}])
def test_non_positive_max_chars_raises(kwargs):
    with pytest.raises(ValueError):
        chunk_markdown("内容", **kwargs)


@pytest.mark.parametrize("ratio", [-0.1, 1.0, 1.5])
def test_out_of_range_overlap_ratio_raises(ratio):
    with pytest.raises(ValueError):
        chunk_markdown("内容", overlap_ratio=ratio)


# ---------------------------------------------------------------- 无结构文本


def test_plain_text_becomes_one_chunk():
    text = "这是一段没有任何标题的纯文本。"
    chunks = _chunk(text)
    assert len(chunks) == 1
    assert chunks[0]["text"] == text
    assert chunks[0]["heading_path"] == []
    assert (chunks[0]["char_start"], chunks[0]["char_end"]) == (0, len(text))


def test_plain_text_longer_than_limit_is_hard_split():
    text = "句子。" * 100  # 300 字，无标题无空行
    chunks = _chunk(text, max_chars=100, overlap_ratio=0)
    assert len(chunks) > 1
    _assert_span_invariant(text, chunks)
    _assert_ordered_and_contiguous(chunks)


# ---------------------------------------------------------------- 按标题切分


def test_splits_at_h1_and_h2():
    text = (
        "# 项目名\n\n"
        "这是简介。\n\n"
        "## 安装\n\n"
        "先装依赖。\n\n"
        "## 用法\n\n"
        "再跑起来。\n"
    )
    chunks = _chunk(text)

    assert len(chunks) == 3
    assert [c["heading_path"] for c in chunks] == [
        ["项目名"],
        ["项目名", "安装"],
        ["项目名", "用法"],
    ]
    _assert_span_invariant(text, chunks)
    _assert_ordered_and_contiguous(chunks)


def test_preamble_before_first_heading_becomes_its_own_chunk():
    """README 开头常有徽章区和一句话简介，它们不属于任何标题"""
    text = "![badge](x.svg)\n\n一句话简介。\n\n# 标题\n\n正文。\n"
    chunks = _chunk(text)
    assert chunks[0]["heading_path"] == []
    assert "一句话简介" in chunks[0]["text"]
    _assert_span_invariant(text, chunks)


def test_heading_path_is_a_breadcrumb_across_levels():
    text = "# 一\n\n甲\n\n## 二\n\n乙\n\n### 三\n\n丙\n"
    chunks = _chunk(text)
    paths = [c["heading_path"] for c in chunks]
    assert ["一"] in paths
    assert ["一", "二"] in paths
    # h3 超出默认主边界（h1-h2），所以它归在「二」这一段里，不单独成块
    assert ["一", "二", "三"] not in paths


def test_h3_becomes_a_boundary_only_when_needed():
    """h3 默认不是主边界；只有当 h2 段超长时才降一级用它来切

    构造要点：h2 段整体要超过 max_chars（否则根本不会降级），但每个 h3 小节
    要**小于** max_chars（否则小节内部还会被继续切，断言就不是 3 块了）。
    """
    body = "甲" * 200
    text = f"## 大节\n\n开篇。\n\n### 小节一\n\n{body}\n\n### 小节二\n\n{body}\n"
    chunks = _chunk(text, max_chars=300)

    assert len(text) > 300
    assert len(chunks) == 3
    assert chunks[1]["heading_path"] == ["大节", "小节一"]
    assert chunks[2]["heading_path"] == ["大节", "小节二"]
    _assert_span_invariant(text, chunks)


# ---------------------------------------------------------------- 代码围栏保护


def test_heading_like_line_inside_fence_is_not_a_boundary():
    """代码块里的 `# 注释` 与 Markdown 标题字面无法区分，必须靠围栏排除"""
    text = (
        "# 项目\n\n"
        "介绍。\n\n"
        "```bash\n"
        "# 这是注释，不是标题\n"
        "echo hi\n"
        "```\n\n"
        "## 安装\n\n"
        "装一下。\n"
    )
    chunks = _chunk(text)

    assert len(chunks) == 2
    for chunk in chunks:
        assert "这是注释" not in "".join(chunk["heading_path"])
    # 围栏内容仍应完整留在第一块里，不能被切掉
    assert "# 这是注释，不是标题" in chunks[0]["text"]
    _assert_span_invariant(text, chunks)


def test_unclosed_fence_swallows_the_rest_of_the_document():
    """围栏未闭合（被采集侧截断的 README 很常见）时，后半段不能再识别出标题"""
    text = "# 标题\n\n内容。\n\n```bash\n# 伪标题\n\necho hi\n"
    chunks = _chunk(text)
    for chunk in chunks:
        assert "伪标题" not in "".join(chunk["heading_path"])
    _assert_span_invariant(text, chunks)


def test_unclosed_fence_does_not_produce_a_giant_chunk():
    """回归：未闭合围栏会让围栏尾巴伸到文末，避让逻辑不能因此把块撑成十几倍

    修复前这里会切出一个上万字的巨块 —— 一块里混了几十个主题，向量被彻底稀释，
    而且白占提示词篇幅。宁可切坏一段代码，也不能产出这种块。
    """
    tail = "正文段落。" * 800  # 4800 字，远超 max_chars
    text = "# 标题\n\n```bash\n# 围栏一直没闭合\n" + tail
    max_chars = 500
    chunks = _chunk(text, max_chars=max_chars, overlap_ratio=0)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk["text"]) <= max_chars * 1.5
    _assert_span_invariant(text, chunks)


def test_contentless_chunk_between_adjacent_headings_is_absorbed():
    """回归：两个标题紧挨着时会产生纯分隔符块（`"\\n\\n---\\n\\n"`）

    这类块没有语义，却会向量化成一个无意义的点，还在检索结果里占名额。它们长度
    也短，但**不能只靠 min_chars 兜** —— min_chars 的合并有容量上限，前一块已经
    接近上限时它就漏网了。
    """
    filler = "正文。" * 200  # 让前一块接近 max_chars，堵死 min_chars 的合并路径
    text = (
        "# 标题\n\n"
        f"## 第一节\n\n{filler}\n\n"
        "---\n\n"
        "## 第二节\n\n内容。\n"
    )
    chunks = _chunk(text, max_chars=500, overlap_ratio=0)

    for chunk in chunks:
        assert not _CONTENTLESS_RE.match(chunk["text"]), f"残留无内容块：{chunk['text']!r}"
    _assert_span_invariant(text, chunks)


def test_no_zero_length_chunk_when_fence_reaches_segment_end():
    """回归：围栏避让把切点正好推到段尾时，不能再补一个零长度的块

    零长度的块会向量化成一个无意义的点，还会在检索结果里占掉一个名额 —— 而且
    因为它「看起来是个正常的块」，排查起来很费劲。
    """
    text = "# 标题\n\n```\n" + "x" * 130  # 段长 139，围栏未闭合、一直伸到段尾
    chunks = _chunk(text, max_chars=100, overlap_ratio=0)

    for chunk in chunks:
        assert chunk["char_end"] > chunk["char_start"], f"出现零长度块：{chunk}"
        assert chunk["text"].strip()
    _assert_span_invariant(text, chunks)


def test_blank_line_inside_fence_is_not_a_paragraph_boundary():
    text = "## 代码\n\n```python\n" + "x = 1\n" * 3 + "\n" + "y = 2\n" * 3 + "```\n"
    chunks = _chunk(text, max_chars=40)
    _assert_span_invariant(text, chunks)
    # 围栏内的空行被跳过后，整块代码至少还在同一块里（除非围栏本身超过 max_chars）
    joined = "".join(c["text"] for c in chunks)
    assert "x = 1" in joined and "y = 2" in joined


# ---------------------------------------------------------------- 逐级降级


def test_oversized_section_falls_back_to_paragraph_grouping():
    para = "甲" * 200
    text = f"## 大节\n\n{para}\n\n{para}\n\n{para}\n"
    chunks = _chunk(text, max_chars=300, overlap_ratio=0)

    assert len(chunks) == 3
    _assert_span_invariant(text, chunks)
    _assert_ordered_and_contiguous(chunks)


def test_hard_split_snaps_to_sentence_boundary():
    """长度硬切必须落在句末，不能把一句话从中间劈开"""
    text = "第一句。第二句。第三句。" * 30  # 360 字，无标题无空行
    chunks = _chunk(text, max_chars=100, overlap_ratio=0)

    assert len(chunks) > 2
    for chunk in chunks[:-1]:
        # 每块都应在中文句号处收尾
        assert text[chunk["char_end"] - 1] == "。"
    _assert_span_invariant(text, chunks)


def test_never_exceeds_max_chars_without_fences():
    """没有围栏强制超长时，每块都不超过 max_chars"""
    text = "\n\n".join(f"第{i}段。" + "甲" * 150 for i in range(10))
    max_chars = 500
    chunks = _chunk(text, max_chars=max_chars, overlap_ratio=0)

    for chunk in chunks:
        assert len(chunk["text"]) <= max_chars
    _assert_span_invariant(text, chunks)


# ---------------------------------------------------------------- 重叠


def test_overlap_pulls_prefix_from_previous_chunk():
    text = "甲" * 300 + "\n\n" + "乙" * 300
    chunks = chunk_markdown(text, max_chars=300, overlap_ratio=0.5, min_chars=100)

    assert len(chunks) == 2
    expected_overlap = int(300 * 0.5)
    assert chunks[1]["char_start"] == chunks[0]["char_end"] - expected_overlap

    # 重叠区就是「前一块的尾巴」与「后一块的开头」共用的那一段。
    # 注意不能断言成「150 个甲」—— 前一块的尾部其实是 148 个甲加一个空行，
    # 重叠是按**字符**算的，不是按段落算的。
    shared = text[chunks[1]["char_start"] : chunks[0]["char_end"]]
    assert len(shared) == expected_overlap
    assert chunks[0]["text"].endswith(shared)
    assert chunks[1]["text"].startswith(shared)

    _assert_span_invariant(text, chunks)
    _assert_ordered_and_contiguous(chunks)


def test_zero_overlap_makes_chunks_contiguous():
    text = "甲" * 300 + "\n\n" + "乙" * 300
    chunks = chunk_markdown(text, max_chars=300, overlap_ratio=0.0, min_chars=100)

    assert len(chunks) == 2
    assert chunks[1]["char_start"] == chunks[0]["char_end"]


def test_first_chunk_has_no_overlap_prefix():
    text = "甲" * 300 + "\n\n" + "乙" * 300
    chunks = chunk_markdown(text, max_chars=300, overlap_ratio=0.5, min_chars=100)
    assert chunks[0]["char_start"] == 0


def test_overlap_is_capped_by_previous_chunk_length():
    """前一块很短时，重叠不能超出它的长度（否则会倒着越过块首）"""
    text = "短。\n\n" + "乙" * 400
    chunks = chunk_markdown(text, max_chars=300, overlap_ratio=0.9, min_chars=1)
    assert chunks[1]["char_start"] >= 0
    _assert_span_invariant(text, chunks)


# ---------------------------------------------------------------- 过短合并


def test_short_chunk_is_merged_into_neighbour():
    """`## License / MIT` 这种块没有语义，留着只会占检索结果的名额"""
    text = "## 甲\n\n甲内容。\n\n## 乙\n\n" + "乙" * 300
    chunks = chunk_markdown(text, min_chars=100)

    assert len(chunks) == 1
    # 合并后沿用后一块的面包屑 —— 内容以后者为主（12 字 vs 300 字）
    assert chunks[0]["heading_path"] == ["乙"]
    _assert_span_invariant(text, chunks)


def test_short_first_chunk_merges_forward():
    """首块过短时没有前驱可并，只能并进后一块"""
    text = "短。\n\n# 大标题\n\n" + "正文。" * 60
    chunks = chunk_markdown(text, min_chars=100)

    assert len(chunks) == 1
    assert chunks[0]["char_start"] == 0
    _assert_span_invariant(text, chunks)


def test_merging_does_not_produce_oversized_chunks():
    """合并不该把块撑到远超上限 —— 允许略超 min_chars，不允许翻倍"""
    text = "\n\n".join(["短。"] + ["甲" * 200] * 3)
    chunks = chunk_markdown(text, max_chars=250, min_chars=100)

    for chunk in chunks:
        assert len(chunk["text"]) <= 250 + 100
    _assert_span_invariant(text, chunks)


# ---------------------------------------------------------------- 综合


def test_realistic_readme_shape():
    """贴近真实 README 的形态：徽章区 + 多级标题 + 代码块 + 超长小节"""
    text = (
        "<div align=\"center\">\n\n"
        "![badge](a.svg) ![badge](b.svg)\n\n"
        "# Awesome Tool\n\n"
        "**一句话简介。**\n\n"
        "## Features\n\n"
        "- 特性一\n- 特性二\n\n"
        "## Installation\n\n"
        "```bash\n"
        "pip install awesome-tool\n"
        "```\n\n"
        "## Usage\n\n"
        "### 基本用法\n\n"
        + "详细说明。" * 80
        + "\n\n### 高级配置\n\n"
        + "配置说明。" * 80
        + "\n\n## License\n\nMIT\n"
    )
    chunks = chunk_markdown(text, max_chars=300)

    assert len(chunks) > 3
    _assert_span_invariant(text, chunks)
    _assert_ordered_and_contiguous(chunks)
    # 全文应被完整覆盖（首块从 0 起，末块到文末）
    assert chunks[0]["char_start"] == 0
    assert chunks[-1]["char_end"] == len(text)


@pytest.mark.parametrize("max_chars", [200, 400, 800, 1200, 2000])
def test_invariant_holds_across_parameters(max_chars):
    text = (
        "# 标题\n\n简介。\n\n## 一\n\n" + "甲" * 900 + "\n\n### 一甲\n\n" + "乙" * 500
        + "\n\n## 二\n\n```py\nx = 1\n```\n\n" + "丙" * 400
    )
    chunks = chunk_markdown(text, max_chars=max_chars)
    _assert_span_invariant(text, chunks)
    _assert_ordered_and_contiguous(chunks)
