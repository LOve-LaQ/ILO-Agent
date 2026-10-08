# RAG Chunk Spec - 块集合的规格与纯函数
"""块向量集合的常量、点 ID 与 payload 组装。

与 `src/services/recommend.py` 的定位一致：**纯函数，无 IO**，便于离线单测。
真正连 Qdrant 的写在 `chunk_store.py` 里。

## 为什么不复用卡片的 `tech_encyclopedia`

卡片向量的输入是「结构化字段拼接」（标题 + 摘要 + 技术栈 + 亮点…），块向量的输入
是 README 原文片段 —— 两者**不是同一个语义空间**。混在一个集合里检索，得到的是
静默错误的相关性：分数看起来正常，但排序没有意义。所以必须独立集合。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Optional, Tuple

# 块向量集合。与 `tech_encyclopedia`（卡片）、`user_profiles` / `learning_history`
# （记忆）并列，互不复用。
COLLECTION = "card_chunks"
VECTOR_SIZE = 1536

# 单次向量化输入的 token 预算。阿里云 text-embedding-v2 的硬上限是 2048，留约 7%
# 余量给「估算偏差」——估算本身不精确，卡着上限送等于赌运气，而超限的代价是
# **整批 400**（批量请求里一条超限，同批 25 条一起失败）。
EMBEDDING_MAX_TOKENS = 1900

# ---------------------------------------------------------------- token 估算
#
# 【为什么必须按**文字种类**分别估】最初的版本只区分「中文 / 其他」两类，把
# 其他一律按字符数除以 3.5 估。这在纯英文上没问题，但在韩文上错得离谱：
# 韩文实测 **1.24 token/字符**，被当成「其他」估成 0.286 —— 低估 4 倍多。
# 后果是估算说 648 token，实际 2000+，请求打到 API 被 400 拒绝，而这是**批量
# 请求**，25 条里有一条超限就整批失败，整张卡 60 个块一个都写不进去。
#
# 触发条件是「语料里有韩文」：942 篇里有 23 篇含韩文、21 篇含日文假名。
# 这类错误只在真实语料上才暴露，构造测试永远测不到。
#
# 【系数怎么来的】2026-10-06 逐语言实测千问 text-embedding-v2：
#
#   文字        实测 token/字符        本表取值    说明
#   韩文        1.24 ~ 1.63            1.75       取实测**上界**再留余量
#   日文假名     0.812 ~ (混韩文时更高)  1.10       同上
#   中日汉字     0.472 ~ 0.55           0.60       同上
#   其他(英/码)  0.202 ~ 0.44           0.35       同上
#
# 【为什么韩文取 1.75 这么大的余量 —— 这是踩过的坑】第一次只在 500 字符的均匀
# 韩文样本上测，得到 1.24 token/字符，于是取 1.40。结果真实块（1,094 个韩文 +
# 890 个其他混排）实测是 **1.63**，估算仍是偏低，请求照样被 400 拒。
# 原因是韩文音节的 token 数随上下文浮动很大（同一个字在词首/词尾、与拉丁字母
# 相邻时，切分结果都不同），均匀样本测不出上界。
#
# 教训：**token 密度不是文本的固有属性**，它依赖上下文，所以短样本上测出的
# 密度只能当参考，系数必须往上取。反过来，代价只是「更早截断」——
# 而这个语料里只有 0.01% 的块会触发截断，余量给足几乎不花钱。
#
# 【为什么「其他」取 0.35 而不是按英文的 0.202】README 里大量是 URL、代码
# 标识符、文件路径、markdown 符号，实测 markdown 代码块是 0.440 token/字符，
# 远高于自然英文的 0.202。按 0.202 估会让代码块偷偷超限。
#
# ⚠️ **本表只能有一份，别在文件别处再写一遍**。这个 bug 真的发生过：修系数时
# 在下面又留了一份旧表（1.40/0.95/0.55/0.30），Python 后赋值覆盖前赋值，于是
# 「改好的系数」从未生效 —— 而全量回填照样跑通了，因为 `SHRINK_LADDER` 的
# 逐级截断兜住了底。**兜底机制会让配置错误变得不可见**，这是最难发现的一类 bug。
# `tests/test_chunk_spec.py::test_token_coefficient_table_is_not_shadowed` 钉住了这条。
_TOKENS_PER_CHAR = {
    "hangul": 1.75,
    "kana": 1.10,
    "cjk": 0.60,
    "other": 0.35,
}
_HANGUL_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")
_KANA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ffff]")

# 标题在向量输入里的分隔符。用「—」而非换行：模型看到的是「标题 — 正文」这种
# 连续语境，比「标题\n\n正文」更像一句话，也避免被当成两个独立片段。
_TITLE_SEP = " — "

__all__ = [
    "COLLECTION",
    "VECTOR_SIZE",
    "EMBEDDING_MAX_TOKENS",
    "SHRINK_LADDER",
    "build_chunk_payload",
    "build_embedding_text",
    "chunk_point_id",
    "estimate_tokens",
    "shrink_by_length",
]


def estimate_tokens(text: str) -> int:
    """粗略估算文本的 token 数（按文字种类分别估）

    只为「会不会超限」这一个判断服务，不追求精确 —— 真正的 token 数以 API 的
    `usage` 字段为准，入库脚本会把那个数字汇总出来。

    覆盖韩文 / 日文假名 / 中日汉字 / 其他四类。**不能只区分中英两类** ——
    韩文 1.24 token/字符、英文 0.202，差 6 倍；混在一起估，韩文块必然超限。
    """
    if not text:
        return 0
    hangul = len(_HANGUL_RE.findall(text))
    kana = len(_KANA_RE.findall(text))
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - hangul - kana - cjk
    return (
        int(
            hangul * _TOKENS_PER_CHAR["hangul"]
            + kana * _TOKENS_PER_CHAR["kana"]
            + cjk * _TOKENS_PER_CHAR["cjk"]
            + other * _TOKENS_PER_CHAR["other"]
        )
        + 1
    )


def build_embedding_text(
    chunk: Dict[str, Any],
    *,
    title: str = "",
    max_tokens: int = EMBEDDING_MAX_TOKENS,
) -> Tuple[str, bool]:
    """拼出**送进向量模型**的文本，返回 `(文本, 是否被截断)`

    ## 为什么不能直接 embed 块正文

    README 的小节标题高度通用 —— `Installation`、`Usage`、`License`、`Contributing`
    几乎每个仓库都有。只 embed 正文，这些块在向量空间里几乎重合，用户问「怎么安装」
    时会把几百个仓库的 `Installation` 一起召回，而**区分度全在仓库名和一句话简介上**。
    所以前缀必须带上标题（以及小节路径）。

    ## 为什么标题占比大也不怕

    块越长，标题在整段里的权重越低。2000 字符的块里标题约占 1%，可以忽略；
    而恰恰是「Installation」这种 30 字符的短块需要标题撑起语义。前缀的作用天然
    随块长衰减，不需要额外做加权。

    ## 为什么不用整块原文

    与 payload 不带正文同理，但这里的原因不同：正文进的是**向量模型的输入**，
    不进索引。检索命中后要正文时按 `card_id` + `char_start` / `char_end`
    回 PostgreSQL 取 —— 向量库只负责「找到」，不负责「存放」。
    """
    text = chunk.get("text") or ""
    heading_path = [h for h in (chunk.get("heading_path") or []) if h]
    breadcrumb = " › ".join(heading_path)

    prefix_parts = [p for p in (title.strip(), breadcrumb) if p]
    prefix = _TITLE_SEP.join(prefix_parts)
    full = f"{prefix}\n\n{text}" if prefix else text

    if max_tokens <= 0 or estimate_tokens(full) <= max_tokens:
        return full, False

    # 截断：按字符比例粗算一个上界再回退，避免逐字符试算。
    # 从**尾部**砍 —— 前缀（标题/小节）和块的开头承载的信息密度最高。
    ratio = max_tokens / estimate_tokens(full)
    cut = max(1, int(len(full) * ratio * 0.95))
    return full[:cut], True


# token 预算是 1900，硬上限 2048。预留 30 的余量给估算误差。
_API_HARD_LIMIT = 2048
_API_SAFETY_MARGIN = 30


def shrink_by_length(
    text: str,
    *,
    lo_limit: int = 1,
    target_ratio: float = 0.92,
) -> str:
    """按比例给出一个**候选**长度（不做验证，验证由调用方按自己的下游做）

    ## 为什么这个函数存在，而不是直接估完就发

    见模块顶部关于 token 密度依赖上下文的说明。这里只负责「砍到哪」这个纯计算，
    「砍完能不能过」交给调用方（它才知道下游是谁、怎么写请求）。分成两半的好处是
    纯函数可离线单测，而验证逻辑不必污染 `chunk_spec`。

    Args:
        text: 待缩文本
        lo_limit: 最短保留字符数
        target_ratio: 目标长度相对原长的比例（留余量，别卡着边界）
    """
    if not text:
        return text
    length = max(lo_limit, int(len(text) * target_ratio))
    return text[:length]


# 供调用方按需试的候选比例：从宽松到激进。
# 用固定档位而不是二分，是因为**每次验证都要真的发一次请求**（付费），
# 而超长块在真实语料里只占 0.007% —— 二分要 11 次调用，档位最多 5 次。
SHRINK_LADDER = (0.85, 0.70, 0.55, 0.40, 0.25)


def chunk_point_id(card_id: str, ordinal: int) -> int:
    """块的点 ID：`md5("{card_id}:{ordinal}")` 前 16 位转整数

    Qdrant 要求 ID 是整数或 UUID。用 `(card_id, ordinal)` 而非纯 ordinal 组合，
    是为了让「同一张卡的同一序号」天然幂等 —— 重新切分入库时按 ID 覆盖，
    不会产生重复点。与 `TechKnowledgeBase.upsert` 的取 ID 方式保持一致。
    """
    digest = hashlib.md5(f"{card_id}:{ordinal}".encode()).hexdigest()
    return int(digest[:16], 16)


def build_chunk_payload(
    *,
    card_id: str,
    chunk: Dict[str, Any],
    source_url: Optional[str] = None,
    title: Optional[str] = None,
    embedding_fingerprint: Optional[str] = None,
) -> Dict[str, Any]:
    """组装块的 Qdrant payload

    **刻意不放整块原文**：单块上千字符，几十万个块塞进 payload 会撑爆索引并拖慢
    scroll（这与 `tech_knowledge.upsert` 排除 `raw_content` 是同一个理由）。
    检索命中后要原文时，按 `card_id` + `char_start` / `char_end` 回 PostgreSQL 取。

    必须带上 `embedding_fingerprint`：换向量模型后新旧向量不在同一语义空间，
    靠这个字段才能识别出「哪些块需要重算」。卡片向量当年漏了这个字段，块这边
    不能再欠。
    """
    return {
        "card_id": card_id,
        "ordinal": int(chunk.get("ordinal", 0)),
        "heading_path": list(chunk.get("heading_path") or []),
        "char_start": int(chunk.get("char_start", 0)),
        "char_end": int(chunk.get("char_end", 0)),
        "source_url": source_url or "",
        "title": title or "",
        "embedding_fingerprint": embedding_fingerprint or "",
    }
