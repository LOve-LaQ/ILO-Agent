# Tests - 块集合规格（纯函数）
"""`src/modules/rag/chunk_spec.py` 的离线单测。

这个模块里三个函数分别守着三条容易静默出错的性质：

1. **点 ID 的幂等性** —— `(card_id, ordinal)` 决定 ID。ID 一变，重跑就会写出
   重复点而不是覆盖，库里出现同一块的多个副本，检索结果里同一段文字反复出现。
2. **token 估算偏保守** —— 估算偏低的后果不是「估不准」，而是超限请求真的打到
   API，而批量请求里一条超限就整批 400（25 个块一起白跑）。
3. **向量化输入带标题前缀** —— 少了前缀，README 里那些通用的 `Installation` /
   `Usage` 小节会在向量空间里互相重合，检索质量下降但不报任何错。
"""

import pytest

from src.modules.rag.chunk_spec import (
    COLLECTION,
    EMBEDDING_MAX_TOKENS,
    SHRINK_LADDER,
    VECTOR_SIZE,
    build_chunk_payload,
    build_embedding_text,
    chunk_point_id,
    estimate_tokens,
    shrink_by_length,
)


def _chunk(text: str, ordinal: int = 0, heading_path=None) -> dict:
    return {
        "text": text,
        "ordinal": ordinal,
        "heading_path": heading_path or [],
        "char_start": 0,
        "char_end": len(text),
    }


# ---------------------------------------------------------------- 点 ID


def test_point_id_is_deterministic():
    """同一 (card_id, ordinal) 必须永远得到同一个 ID —— 幂等全靠这条"""
    assert chunk_point_id("gh-1", 3) == chunk_point_id("gh-1", 3)


def test_point_id_differs_by_card_and_ordinal():
    base = chunk_point_id("gh-1", 0)
    assert base != chunk_point_id("gh-2", 0)
    assert base != chunk_point_id("gh-1", 1)


def test_point_id_is_unsigned_64bit():
    """Qdrant 的整数 ID 是 u64；超出范围客户端会直接报错，不能靠运气"""
    for card_id in ("gh-1", "gh-999999999", "news-001", "x" * 64):
        for ordinal in (0, 1, 999):
            point_id = chunk_point_id(card_id, ordinal)
            assert 0 <= point_id < 2**64


def test_point_id_card_id_boundary_is_not_ambiguous():
    """`"a:1"` 和 `"a"`+`1` 这类拼接歧义必须被分隔符挡住

    直接把 card_id 和 ordinal 首尾相接，`("a1", 2)` 和 `("a", 12)` 会算出同一个
    ID —— 两张不同的卡互相覆盖对方的块，而且完全静默。
    """
    assert chunk_point_id("a1", 2) != chunk_point_id("a", 12)


# ---------------------------------------------------------------- token 估算


@pytest.mark.parametrize("text", ["", None])
def test_estimate_tokens_empty(text):
    assert estimate_tokens(text) == 0


def test_estimate_tokens_is_positive_for_tiny_text():
    assert estimate_tokens("a") >= 1


def test_chinese_costs_more_tokens_per_char_than_ascii():
    """同样长度的中英文，中文的 token 数必须更高

    这条一旦反过来，说明系数被写反了 —— 而后果是中文块超限、整批 400。
    """
    assert estimate_tokens("测" * 100) > estimate_tokens("a" * 100)


def test_estimate_tokens_is_conservative_for_english():
    """英文估算必须明显偏高，但别离谱

    纯英文长文本实测约 **0.202 token/字符**（含 markdown 代码块时约 0.44）。
    本表对「其他」取 0.30 —— 比自然英文高约 1.5 倍，是为了让 URL / 代码标识符 /
    路径这些「切得碎」的内容不偷偷超限。

    下限的意义是**方向**：估算必须站在偏高一侧。偏高的代价只是更早截断，
    偏低会让超限请求真的打到 API，而批量请求是一条超限、整批 400。
    """
    text = "a" * 1000
    # 实测密度 0.202 → 1000 字符约 202 token；估算应当明显高于它
    assert estimate_tokens(text) >= 250
    # 但也别高到把正常块误判成超限（0.30 上限对应 400 token 左右）
    assert estimate_tokens(text) <= 400


def test_korean_costs_far_more_than_english():
    """韩文实测 1.24 token/字符，是英文（0.202）的 6 倍多

    【为什么单独为韩文写一条】最初只区分「中文/其他」两类，韩文被当成「其他」
    按 0.286 token/字符估 —— 低估 4 倍多。后果是估算说 648 token、实际 2000+，
    批量请求整批被 400 拒绝，**整张卡的 60 个块一个都写不进去**。
    942 篇语料里有 23 篇含韩文，这不是理论风险。
    """
    korean = "가" * 1000
    english = "a" * 1000
    assert estimate_tokens(korean) >= estimate_tokens(english) * 4


def test_korean_estimation_matches_measured_density():
    """韩文实测 1.24 ~ 1.63；本表取 1.75，估算应**不低于实测上界**

    断言写成「不低于 1.63」而不是「落在 [1.24, 1.6] 区间内」：后者把**实测上界
    当成了估算的上限**，方向正好反了 —— 估算必须站在实测之上，否则超限请求会
    真的打到 API。这条断言曾经就是按区间写的，所以 1.40 那个偏低的系数也能通过，
    把 bug 放了过去。
    """
    per_char = estimate_tokens("가" * 2000) / 2000
    assert per_char >= 1.63


def test_token_coefficient_table_is_not_shadowed():
    """系数表只能有一份，且必须是「偏保守」的那组值

    【为什么单独钉这一条】这个 bug 真的发生过：修完系数后文件下面还留着一份旧表，
    Python 后赋值覆盖前赋值，**改好的值从未生效**。而全量回填照样跑通 ——
    因为 `SHRINK_LADDER` 的逐级截断兜住了底。**有兜底的地方，配置错误不会报错，
    只会静默地让兜底天天触发。**

    所以这里直接断言生效值，而不是断言「估算结果在某个区间」—— 区间断言会被
    同方向的错误值骗过去。
    """
    from src.modules.rag import chunk_spec

    assert chunk_spec._TOKENS_PER_CHAR == {
        "hangul": 1.75,
        "kana": 1.10,
        "cjk": 0.60,
        "other": 0.35,
    }
    # 四个桶一个都不能少：少一个键会在 estimate_tokens 里 KeyError，
    # 但少一**类文字**（比如删掉 hangul）不会报错，只会让韩文落进 other 被低估。
    assert set(chunk_spec._TOKENS_PER_CHAR) == {"hangul", "kana", "cjk", "other"}
    # 顺序约束：韩文 > 假名 > 汉字 > 其他。写反了不会报错，但估算会整体失真。
    table = chunk_spec._TOKENS_PER_CHAR
    assert table["hangul"] > table["kana"] > table["cjk"] > table["other"]


def test_kana_costs_more_than_english():
    """日文假名实测 0.812 token/字符，约为英文的 4 倍"""
    kana = "あ" * 1000
    assert estimate_tokens(kana) >= estimate_tokens("a" * 1000) * 3


def test_mixed_script_text_sums_all_buckets():
    """混排文本四个桶都要算到，不能只认其中一类

    切分出的块经常是「韩文标题 + 中文说明 + 英文代码」，只按一类估必然偏。
    """
    korean = estimate_tokens("가" * 500)
    chinese = estimate_tokens("中" * 500)
    english = estimate_tokens("a" * 500)
    mixed = estimate_tokens("가" * 500 + "中" * 500 + "a" * 500)
    # 允许 ±5% 的取整误差
    assert abs(mixed - (korean + chinese + english)) / mixed < 0.05


def test_estimate_tokens_scales_linearly():
    one = estimate_tokens("a" * 1000)
    two = estimate_tokens("a" * 2000)
    assert two > one * 1.5


# ---------------------------------------------------------------- 向量化输入


def test_embedding_text_has_no_prefix_when_no_title_and_no_heading():
    text, truncated = build_embedding_text(_chunk("正文"))
    assert text == "正文"
    assert truncated is False


def test_embedding_text_prepends_title_and_breadcrumb():
    text, _ = build_embedding_text(
        _chunk("安装步骤……", heading_path=["项目名", "安装"]), title="owner/repo"
    )
    assert text.startswith("owner/repo — 项目名 › 安装")
    assert text.endswith("安装步骤……")


def test_embedding_text_works_with_only_title():
    text, _ = build_embedding_text(_chunk("正文"), title="owner/repo")
    assert text == "owner/repo\n\n正文"


def test_embedding_text_ignores_empty_heading_entries():
    """切分器可能产出 `["", "安装"]` 这种带空项的小节路径，不能让分隔符悬空"""
    text, _ = build_embedding_text(_chunk("正文", heading_path=["", "安装"]), title="t")
    assert text.startswith("t — 安装")
    assert "›" not in text.split("\n")[0]


def test_short_chunk_is_not_truncated():
    text, truncated = build_embedding_text(_chunk("短正文"))
    assert truncated is False
    assert text == "短正文"


def test_over_budget_text_is_truncated_and_flagged():
    long_text = "a" * 100_000
    text, truncated = build_embedding_text(_chunk(long_text), title="t")
    assert truncated is True
    assert len(text) < len(long_text)
    assert estimate_tokens(text) <= EMBEDDING_MAX_TOKENS * 1.1


def test_truncation_keeps_the_prefix():
    """从尾部砍：标题和小节路径承载的信息密度最高，必须先保住"""
    text, truncated = build_embedding_text(
        _chunk("a" * 100_000, heading_path=["小节"]), title="owner/repo"
    )
    assert truncated is True
    assert text.startswith("owner/repo — 小节")


def test_max_tokens_zero_disables_truncation():
    long_text = "a" * 100_000
    text, truncated = build_embedding_text(_chunk(long_text), max_tokens=0)
    assert text == long_text
    assert truncated is False


# ---------------------------------------------------------------- payload


def test_payload_carries_span_and_provenance():
    payload = build_chunk_payload(
        card_id="gh-1",
        chunk=_chunk("x" * 10, ordinal=2, heading_path=["A", "B"]),
        source_url="https://example.com",
        title="owner/repo",
        embedding_fingerprint="aliyun:text-embedding-v2:1536",
    )
    assert payload == {
        "card_id": "gh-1",
        "ordinal": 2,
        "heading_path": ["A", "B"],
        "char_start": 0,
        "char_end": 10,
        "source_url": "https://example.com",
        "title": "owner/repo",
        "embedding_fingerprint": "aliyun:text-embedding-v2:1536",
    }


def test_payload_excludes_chunk_text():
    """payload 里绝不能有正文 —— 单块上千字符，几十万块会撑爆索引

    这条是「Qdrant 只负责找到、PostgreSQL 才存正文」这个分工的守门人。
    """
    payload = build_chunk_payload(card_id="gh-1", chunk=_chunk("很长的正文" * 100))
    assert "text" not in payload
    assert all(not isinstance(v, str) or len(v) < 200 for v in payload.values())


def test_payload_defaults_are_empty_strings_not_none():
    """Qdrant 的 payload 里 None 和 "" 在过滤时行为不同；统一成 "" 免得踩坑"""
    payload = build_chunk_payload(card_id="gh-1", chunk=_chunk("正文"))
    assert payload["source_url"] == ""
    assert payload["title"] == ""
    assert payload["embedding_fingerprint"] == ""
    assert payload["heading_path"] == []


def test_payload_copies_heading_path():
    """必须复制：调用方拿到 payload 后再改原 chunk，不能影响已写入的 payload"""
    heading_path = ["A"]
    payload = build_chunk_payload(card_id="gh-1", chunk=_chunk("正文", heading_path=heading_path))
    heading_path.append("B")
    assert payload["heading_path"] == ["A"]


def test_collection_constants():
    """块集合必须独立于卡片集合（tech_encyclopedia），不能复用"""
    assert COLLECTION == "card_chunks"
    assert VECTOR_SIZE == 1536


# ---------------------------------------------------------------- 超长截断


def test_shrink_by_length_shortens_proportionally():
    text = "a" * 1000
    assert len(shrink_by_length(text, target_ratio=0.5)) == 500


def test_shrink_by_length_respects_lo_limit():
    """比例算出来低于下限时，保住下限 —— 不能让二分退化成空串"""
    text = "a" * 10
    assert len(shrink_by_length(text, lo_limit=4, target_ratio=0.01)) == 4


def test_shrink_by_length_is_monotonic():
    """档位越激进、长度越短 —— 调用方是靠依次试档位收敛的"""
    text = "a" * 1000
    lengths = [len(shrink_by_length(text, target_ratio=r)) for r in SHRINK_LADDER]
    assert lengths == sorted(lengths, reverse=True)


def test_shrink_ladder_is_descending_and_reaches_short_enough():
    """档位必须递减，且最低档要足够激进（否则砍不动超长块）

    最低档 0.25：一个 4000 字符的块砍到 1000 字符。对本语料最长的块
    （2,924 字符）来说，0.25 档留 731 字符，足以落回预算内。
    """
    assert list(SHRINK_LADDER) == sorted(SHRINK_LADDER, reverse=True)
    assert SHRINK_LADDER[0] == 0.85
    assert SHRINK_LADDER[-1] <= 0.25


def test_shrink_by_length_handles_empty():
    assert shrink_by_length("") == ""
