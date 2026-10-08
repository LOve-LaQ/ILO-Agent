# RAG Chunker - Markdown 切分
"""把 README 原文切成可检索的块。

## 为什么按结构切，而不是按固定长度

固定长度最省事，但它会把一句话、一个论证从中间切断，检索出来是没头没尾的，模型
也没法用。而 Markdown 的标题层级是**作者自己划好的语义边界** —— 按它切最省事也最
准。所以这里做三层逐级降级，目的是**尽量晚地动用长度硬切**：

    按标题切（h1-h2） → 单块仍超长则降一级（h3、h4…） → 按段落 → 最后才按长度

## 默认参数从哪来（不是拍脑袋）

本地库 942 篇 README 的体检结果（`backend/audit_corpus_for_rag.py`）：

- **零标题文档仅 0.4%** —— 按结构切对 99.6% 的文档可行，所以它是首选而非备选；
- 标题层级 **h2 占 56.8%、h3 占 26.1%** —— 主边界取 h1-h2，超长再降 h3；
- 长度分布 **2k~8k 占 36%、8k~30k 占 49%** —— 单块上限取 2000 字。再大就会让一块里
  混进多个主题，相似度被「平均」掉（一块里八句话只有一句相关，整块向量就被稀释了）。

## 不变式（下游依赖它）

**每块的 `text` 必须等于原文按 `[char_start, char_end)` 切片的结果。**

这条不变式是任务 B 的评测能跨切分策略对齐标签的前提 —— 有了它，「检索结果有没有
覆盖答案所在的原文区间」才能被机械判定，不必人工看。因此块边界一律不裁剪空白：
一旦 `strip()` 了首尾，切片就对不上了。

## 与代码围栏的关系

README 的代码块里经常出现 `# 这看起来像标题` 的行，字面上与 Markdown 标题无法区分。
不排除围栏就会把一行注释当成标题，切出无意义的块。所以标题识别、段落切分、长度
切分三处都避开了围栏内部。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_MAX_CHARS = 2000
DEFAULT_OVERLAP_RATIO = 0.15
DEFAULT_MIN_CHARS = 100
DEFAULT_MAX_HEADING_LEVEL = 2

# 标题行：行首 1~6 个 `#`，后跟空白与标题文本
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
# 段落边界：空行（允许行内残留空白）
_PARAGRAPH_RE = re.compile(r"\n[ \t]*\n")
# 代码围栏起止：``` 或 ~~~
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})", re.MULTILINE)
# 句子边界：中文句末标点 / 换行 / 英文句末标点后跟空白
_SENTENCE_END_RE = re.compile(r"[。！？；\n]|[.!?](?=\s|$)")
# 「无内容」判定：整块只由空白和 Markdown 分隔符组成。
# 两个标题紧挨着时（`## A\n\n---\n\n## B`）会产生只有 `"\n\n---\n\n"` 的块 —— 它
# 没有语义，却会向量化成一个无意义的点，并在检索结果里占掉一个名额。这类块必须
# 并掉，不能只靠 min_chars：它们长度是够短的，但 min_chars 的合并有容量上限，
# 遇到前一块已经接近上限时就会漏网。
_CONTENTLESS_RE = re.compile(r"^[\s\-=*_>#|`~+.]*$")

# 长度硬切时，允许回退到句子边界的最大比例（相对 max_chars）。
# 回退太多会让块碎得没有信息量，太少又常常回退不到真正的句末。
_SENTENCE_LOOKBACK_RATIO = 0.4
# 围栏避让阈值：退到围栏之前若会让本块不足这个比例，就改为推到围栏之后。
# 宁可块略超长，也不要切出一个只有两行的碎片。
_FENCE_BACKOFF_MIN_RATIO = 0.5
# 围栏避让的**上限**：推到围栏之后若会把块撑到超过 max_chars 的这个倍数，就别推了，
# 直接在围栏内部切。没有这条上限时，一个未闭合的围栏（被采集侧截断的 README 很
# 常见，`_fence_ranges` 会把围栏尾巴伸到文末）会让切点一路跳到文末，切出上万字的
# 巨块 —— 那既污染向量（一块里混了几十个主题），也白占提示词篇幅。
_FENCE_FORWARD_MAX_RATIO = 1.5


@dataclass(frozen=True)
class _Segment:
    """原文中的一个区间及其所属的标题面包屑（半开区间 `[start, end)`）"""

    start: int
    end: int
    heading_path: tuple[str, ...]


# ---------------------------------------------------------------- 围栏识别


def _fence_ranges(text: str) -> list[tuple[int, int]]:
    """返回代码围栏区间 `[start, end)`，用于把围栏内部排除在切分边界之外

    围栏未闭合时（README 里很常见，尤其是被采集侧截断的文档）截到文末 —— 否则
    后面整段文本都会被当成围栏外，标题识别又会把代码注释当标题。
    """
    ranges: list[tuple[int, int]] = []
    opened_at: int | None = None
    for match in _FENCE_RE.finditer(text):
        if opened_at is None:
            opened_at = match.start()
        else:
            ranges.append((opened_at, match.end()))
            opened_at = None
    if opened_at is not None:
        ranges.append((opened_at, len(text)))
    return ranges


def _inside_fence(fences: list[tuple[int, int]], pos: int) -> bool:
    """位置是否落在围栏**内部**（恰好等于围栏起止点算外部，那里可以安全切分）"""
    for start, end in fences:
        if start < pos < end:
            return True
    return False


# ---------------------------------------------------------------- 标题切分


def _headings(
    text: str,
    fences: list[tuple[int, int]],
    start: int,
    end: int,
    level: int | None = None,
) -> list[tuple[int, int, int, str]]:
    """收集 `[start, end)` 内的标题，返回 `(标题起点, 标题终点, 层级, 文本)`

    围栏内部的伪标题一律剔除 —— 这是本模块最容易出错的地方。
    """
    found = []
    for match in _HEADING_RE.finditer(text, start, end):
        if _inside_fence(fences, match.start()):
            continue
        heading_level = len(match.group(1))
        if level is not None and heading_level != level:
            continue
        found.append((match.start(), match.end(), heading_level, match.group(2).strip()))
    return found


def _split_by_heading(
    text: str, fences: list[tuple[int, int]], max_level: int
) -> list[_Segment]:
    """按 `level <= max_level` 的标题把全文切成段，并维护标题面包屑

    段是**首尾相接、完整覆盖全文**的：第一段从 0 开始，最后一段到文末结束。
    这个覆盖性是后面所有区间运算（尤其重叠）能成立的基础。
    """
    headings = [h for h in _headings(text, fences, 0, len(text)) if h[2] <= max_level]
    if not headings:
        return [_Segment(0, len(text), ())]

    segments: list[_Segment] = []
    if headings[0][0] > 0:
        # 首个标题之前的内容（README 的徽章区、一句话简介）单独成段
        segments.append(_Segment(0, headings[0][0], ()))

    stack: list[tuple[int, str]] = []
    for index, (start, _end, level, title) in enumerate(headings):
        # 同级或更高级的标题出现，说明前面那些标题的作用域已经结束
        while stack and stack[-1][0] >= level:
            stack.pop()
        path = tuple(t for _, t in stack) + (title,)
        stack.append((level, title))
        end = headings[index + 1][0] if index + 1 < len(headings) else len(text)
        segments.append(_Segment(start, end, path))

    return segments


def _span_split_by_heading(
    text: str, fences: list[tuple[int, int]], seg: _Segment, level: int
) -> list[_Segment]:
    """在段内按**更深一级**的标题再切；返回 >1 个才说明真的切动了"""
    headings = [
        h
        for h in _headings(text, fences, seg.start, seg.end, level=level)
        # 段首那个标题是自己，不能拿它当边界，否则会切出一个空块
        if h[0] > seg.start
    ]
    if not headings:
        return [seg]

    parts: list[_Segment] = []
    cursor = seg.start
    path = seg.heading_path
    for start, _end, _level, title in headings:
        parts.append(_Segment(cursor, start, path))
        cursor = start
        path = seg.heading_path + (title,)
    parts.append(_Segment(cursor, seg.end, path))
    return parts


# ---------------------------------------------------------------- 段落与长度切分


def _span_group_paragraphs(
    text: str, fences: list[tuple[int, int]], seg: _Segment, max_chars: int
) -> list[_Segment]:
    """按段落贪心分组，每组尽量装满但不超过 max_chars

    落在围栏内部的空行不作为段落边界 —— 否则会把一段代码从中间拆开，切出来的
    碎片既读不懂也检索不准。
    """
    bounds = [seg.start]
    for match in _PARAGRAPH_RE.finditer(text, seg.start, seg.end):
        if _inside_fence(fences, match.start()):
            continue
        bounds.append(match.end())
    bounds.append(seg.end)

    spans = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
    spans = [(s, e) for s, e in spans if e > s]
    if len(spans) <= 1:
        return [seg]

    parts: list[_Segment] = []
    cursor_start, cursor_end = spans[0]
    for start, end in spans[1:]:
        if end - cursor_start <= max_chars:
            cursor_end = end
        else:
            parts.append(_Segment(cursor_start, cursor_end, seg.heading_path))
            cursor_start, cursor_end = start, end
    parts.append(_Segment(cursor_start, cursor_end, seg.heading_path))
    return parts


def _avoid_fence(
    fences: list[tuple[int, int]], start: int, cut: int, end: int, max_chars: int
) -> int:
    """把落在围栏内部的切点挪出去

    优先退到围栏之前（保持块不超长）；退得太少会让本块碎得没有信息量，就改为推到
    围栏之后。但推到围栏之后有个**上限**：围栏本身比 max_chars 还长时（未闭合的
    围栏会一直伸到文末），推进去等于把块撑成十几倍，那时只能退回原地、在围栏内部
    切 —— 切坏一段代码，好过产出一个上万元的巨块。
    """
    for fence_start, fence_end in fences:
        if fence_start < cut < fence_end:
            if fence_start - start >= (cut - start) * _FENCE_BACKOFF_MIN_RATIO:
                return fence_start
            forward = min(fence_end, end)
            if forward - start <= max_chars * _FENCE_FORWARD_MAX_RATIO:
                return forward
            return cut
    return cut


def _snap_to_sentence(
    text: str, fences: list[tuple[int, int]], start: int, cut: int, max_chars: int
) -> int:
    """把切点回退到最近的句子边界

    固定长度最讨厌的地方就是「把一句话从中间切断」。回退到句末，至少保证每块
    开头结尾是完整的句子。
    """
    floor = start + int(max_chars * (1 - _SENTENCE_LOOKBACK_RATIO))
    if cut <= floor:
        return cut

    best: int | None = None
    for match in _SENTENCE_END_RE.finditer(text, floor, cut):
        pos = match.end()
        if pos <= start or _inside_fence(fences, pos):
            continue
        best = pos
    return best if best is not None else cut


def _span_split_by_length(
    text: str, fences: list[tuple[int, int]], seg: _Segment, max_chars: int
) -> list[_Segment]:
    """最后兜底：按长度硬切。只在文本完全没有结构时才走到这里"""
    parts: list[_Segment] = []
    cursor = seg.start
    while seg.end - cursor > max_chars:
        cut = _avoid_fence(fences, cursor, cursor + max_chars, seg.end, max_chars)
        cut = _snap_to_sentence(text, fences, cursor, cut, max_chars)
        if cut <= cursor:
            # 兜底：切点没往前走就会死循环。宁可切得难看也要保证推进。
            cut = cursor + max_chars
        parts.append(_Segment(cursor, cut, seg.heading_path))
        cursor = cut
    # 围栏避让可能正好把切点推到段尾（`forward` 恰好等于 `seg.end`），这时 cursor
    # 已经等于 seg.end，再 append 就会多出一个零长度的块 —— 它会向量化成一个无意义
    # 的点，并且在检索结果里占掉一个名额。
    if cursor < seg.end:
        parts.append(_Segment(cursor, seg.end, seg.heading_path))
    return parts


def _split_oversized(
    text: str, fences: list[tuple[int, int]], seg: _Segment, max_chars: int, level: int
) -> list[_Segment]:
    """逐级降级地把超长段切开：更深标题 → 段落 → 长度

    `level > 6` 表示「不再尝试标题」—— 段落分组与长度硬切都必然收敛，所以递归
    一定终止。
    """
    if seg.end - seg.start <= max_chars:
        return [seg]

    if level <= 6:
        parts = _span_split_by_heading(text, fences, seg, level)
        if len(parts) > 1:
            expanded: list[_Segment] = []
            for part in parts:
                expanded.extend(_split_oversized(text, fences, part, max_chars, level + 1))
            return expanded

    parts = _span_group_paragraphs(text, fences, seg, max_chars)
    if len(parts) > 1:
        expanded = []
        for part in parts:
            expanded.extend(_split_oversized(text, fences, part, max_chars, 7))
        return expanded

    return _span_split_by_length(text, fences, seg, max_chars)


# ---------------------------------------------------------------- 合并与重叠


def _is_contentless(text: str) -> bool:
    """块是否只有空白与 Markdown 分隔符 —— 这种块不携带任何语义"""
    return bool(_CONTENTLESS_RE.match(text))


def _merge_short(
    segments: list[_Segment], text: str, min_chars: int, max_chars: int
) -> list[_Segment]:
    """把过短或**无内容**的块并进相邻块

    两类块要并掉：

    1. 按标题切会产生「## License / MIT」这种十几字的块 —— 它没有语义，留着只会
       占检索结果的名额；
    2. 更隐蔽的一类：两个标题紧挨着时产生的纯分隔符块（`"\\n\\n---\\n\\n"`）。它长度
       也短，但**不能只靠 min_chars 兜** —— min_chars 的合并有容量上限，前一块已经
       接近上限时它就漏网了。而它一旦入库，就是一个纯粹的噪声向量。

    允许合并后略微超过 max_chars（上限 `max_chars + min_chars`）：为了消掉一个噪声
    块而多出 100 字，比留一个噪声块划算。无内容的块不受这个上限约束 —— 它只贡献
    几个字符，没有把它单列的理由。
    """
    if not segments:
        return []

    merged: list[_Segment] = [segments[0]]
    for seg in segments[1:]:
        prev = merged[-1]
        contentless = _is_contentless(text[seg.start : seg.end])
        too_short = (seg.end - seg.start) < min_chars
        room = contentless or (seg.end - prev.start) <= max_chars + min_chars
        if (contentless or too_short) and room:
            merged[-1] = _Segment(prev.start, seg.end, prev.heading_path)
        else:
            merged.append(seg)

    # 首块过短时没有前驱可并，改为并进后一块
    if len(merged) > 1:
        first = merged[0]
        first_contentless = _is_contentless(text[first.start : first.end])
        if first_contentless or (first.end - first.start) < min_chars:
            merged.pop(0)
            merged[0] = _Segment(first.start, merged[0].end, merged[0].heading_path)

    return merged


def _with_overlap(
    segments: list[_Segment], text: str, overlap_ratio: float, max_chars: int
) -> list[dict]:
    """给每块加上前一块的尾部重叠，并输出最终结构

    重叠的作用是兜住「答案正好跨在两块交界上」那一类 —— 不重叠的话两块各拿到一半，
    谁都答不了。

    实现上把 `char_start` **前移**覆盖住重叠部分，而不是另存一个前缀字段：这样
    「`text` == 原文切片」的不变式仍然成立，重叠区也顺理成章地算作本块的内容
    （评测时命中判定用的是区间相交，重叠区本来就该算命中）。
    """
    overlap_chars = int(max_chars * overlap_ratio)
    chunks: list[dict] = []

    for index, seg in enumerate(segments):
        start = seg.start
        if index > 0 and overlap_chars > 0:
            prev = segments[index - 1]
            take = min(overlap_chars, prev.end - prev.start)
            start = prev.end - take

        chunks.append(
            {
                "text": text[start : seg.end],
                "heading_path": list(seg.heading_path),
                "char_start": start,
                "char_end": seg.end,
                "ordinal": index,
            }
        )

    return chunks


# ---------------------------------------------------------------- 入口


def chunk_markdown(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
    min_chars: int = DEFAULT_MIN_CHARS,
    max_heading_level: int = DEFAULT_MAX_HEADING_LEVEL,
) -> list[dict]:
    """把 Markdown 原文切成可检索的块

    Args:
        text: 原文（README 快照）
        max_chars: 单块上限（字符数）。默认 2000，来自语料长度分布实测
        overlap_ratio: 相邻块的重叠比例（相对 max_chars）。默认 0.15
        min_chars: 低于此长度的块会被合并进相邻块。默认 100
        max_heading_level: 主切分边界取到第几级标题。默认 2（h1-h2）

    Returns:
        按原文顺序排列的块列表，每项：
            - `text`: 块内容（含与前一块的重叠部分）
            - `heading_path`: 标题面包屑，如 `["快速开始", "安装"]`
            - `char_start` / `char_end`: 在原文中的半开区间
            - `ordinal`: 序号，从 0 开始

        **不变式**：`text == 原文[char_start:char_end]`。

    Raises:
        ValueError: `max_chars` 非正数，或 `overlap_ratio` 不在 `[0, 1)` 内。
    """
    if max_chars <= 0:
        raise ValueError("max_chars 必须为正数")
    if not 0 <= overlap_ratio < 1:
        raise ValueError("overlap_ratio 必须落在 [0, 1) 内")

    if not text or not text.strip():
        return []

    fences = _fence_ranges(text)

    segments = _split_by_heading(text, fences, max_heading_level)

    expanded: list[_Segment] = []
    for seg in segments:
        expanded.extend(
            _split_oversized(text, fences, seg, max_chars, max_heading_level + 1)
        )

    merged = _merge_short(expanded, text, min_chars, max_chars)

    # 安全网：零长度的块会向量化成一个无意义的点，还占掉检索结果的一个名额。
    # 各层切分都已保证不产生空段，这里只是兜底 —— 一旦真的兜住了，说明上面有 bug。
    merged = [seg for seg in merged if seg.end > seg.start]

    return _with_overlap(merged, text, overlap_ratio, max_chars)


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_HEADING_LEVEL",
    "DEFAULT_MIN_CHARS",
    "DEFAULT_OVERLAP_RATIO",
    "chunk_markdown",
]
