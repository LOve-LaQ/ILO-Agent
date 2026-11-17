# Services - 相似推荐装点
"""把 Qdrant 的近邻结果装成前端能直接渲染的「相似仓库」条目（无 IO，便于单测）。

职责边界：入参是「源卡片 payload」+「已按余弦排好序的近邻（payload + score）」，
出参是带理由文案的条目列表。不连 Qdrant、不查数据库 —— 检索与阈值在
`tech_knowledge.similar_items` 里完成，这里只负责**解释为什么相似**。

两个刻意的设计：

1. **小幅概念重排**。纯向量相似度分不清「另一个向量数据库」和「一个恰好在文里
   聊向量数据库的笔记应用」。共享技术概念（`core_concepts`）命中越多，越可能是
   同类项目，因此按命中数给一个极小的加分（每项 0.02，上限 0.06）。幅度刻意压得很低：
   它只能调整「本来就差不多」的几条之间的先后，无法把 0.5 的条目顶到 0.8 之上 ——
   否则就不是相似推荐，而是标签匹配了。
2. **理由必须说人话**。`0.72` 对用户没有任何意义。理由讲的是「凭什么放在这里」：
   共享了哪些概念、或者同属哪个方向。没有理由的相似推荐等于让用户自己猜。
"""

from typing import Any, Dict, List, Optional, Sequence

# 每命中一个共享概念的加分，以及它的封顶（避免标签多的卡片靠堆概念挤进前排）
_SHARED_BONUS = 0.02
# 同方向的加分：略高于单个共享概念。实测里出现过「跨方向 0.582 压过同方向 0.567」
# 的情况 —— 语义上确实都很近，但用户要的是「同类的下一个项目」，方向一致
# 比一个偶然共享的词更能说明问题。幅度仍然只有 0.03：它调顺序，不改准入。
_SAME_CATEGORY_BONUS = 0.03
_SHARED_BONUS_CAP = 0.08
# 理由里最多列几个共享概念：三个以上就变成一长串堆砌，反而读不出重点
_MAX_CONCEPTS_IN_REASON = 2


def _concepts(payload: Dict[str, Any], key: str = "core_concepts") -> List[str]:
    """取卡片上的概念/标签列表，统一成去空、去重、保序的字符串列表"""
    raw = payload.get(key)
    if not raw:
        return []
    seen = set()
    result: List[str] = []
    for value in raw:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _category_label(category: Optional[str]) -> Optional[str]:
    """类别代码 -> 中文标签；未知类别返回 None（前端不展示半截文案）"""
    if not category:
        return None
    try:
        from src.modules.discovery.summary_spec import CATEGORY_LABELS

        return CATEGORY_LABELS.get(category)
    except Exception:  # noqa: BLE001 - 文案表取不到不该让推荐整体失败
        return None


def _language_labels(*payloads: Dict[str, Any]) -> set:
    """收集实现语言名（小写）。

    `core_concepts` 里经常混着 "Python" / "TypeScript" 这类语言名，但它们不是
    「技术概念」：库里绝大多数 Python 项目都会共享 "Python"，写进理由只会让人
    一眼看出这理由是凑的（实测第一条推荐就撞上了这个："共享「Python」技术概念"）。
    语言相同不提供语义信息，同方向那一项已经覆盖了它。
    """
    labels = set()
    for payload in payloads:
        language = str(payload.get("language") or "").strip().lower()
        if language:
            labels.add(language)
    return labels


def _reason(shared: Sequence[str], category_zh: Optional[str], same_category: bool) -> str:
    """一句中文理由：优先说共享概念，其次说同方向，最后退回「语义相近」

    `same_category` 必须显式传入而不是「有没有 category」：跨方向的条目也可能
    带着自己的方向（比如源卡片是 AI、候选是运维）。此时说「同为运维方向」会让
    用户以为源卡片也是运维 —— 理由文案必须描述**两者的关系**，不是候选自己的属性。
    """
    if shared:
        head = "」「".join(shared[:_MAX_CONCEPTS_IN_REASON])
        suffix = "等" if len(shared) > _MAX_CONCEPTS_IN_REASON else ""
        return f"共享「{head}」{suffix}技术概念"
    if same_category and category_zh:
        return f"同为「{category_zh}」方向"
    return "与当前项目语义相近"


def build_similar_items(
    source: Optional[Dict[str, Any]],
    neighbours: Sequence[Dict[str, Any]],
    *,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """装点近邻条目并按「概念命中」微调顺序

    Args:
        source: 源卡片 payload（用于算共享概念与方向；缺失时只是理由变通用）
        neighbours: `[{"payload": {...}, "score": 0.72}]`，已按相似度降序
        limit: 返回条数上限

    返回项字段与 `schemas.discover.SimilarItem` 对齐。
    """
    source = source or {}
    source_concepts = set(_concepts(source))
    source_category = source.get("category")

    scored: List[tuple] = []
    for neighbour in neighbours:
        payload = neighbour.get("payload") or {}
        card_id = str(payload.get("id") or "")
        if not card_id:
            # 没有 id 就点不动（前端靠 card_id 续接学习），留着是个死链
            continue

        score = float(neighbour.get("score") or 0.0)
        language_labels = _language_labels(payload, source)
        shared = [
            c
            for c in _concepts(payload)
            if c in source_concepts and c.lower() not in language_labels
        ]
        same_category = bool(
            payload.get("category") and payload.get("category") == source_category
        )
        bonus = min(len(shared) * _SHARED_BONUS, _SHARED_BONUS_CAP)
        # 同方向再加一点：跨方向的「相似」多半是共享词汇造成的假阳性
        if same_category:
            bonus = min(bonus + _SAME_CATEGORY_BONUS, _SHARED_BONUS_CAP)

        category_zh = _category_label(payload.get("category"))
        scored.append(
            (
                score + bonus,
                {
                    "card_id": card_id,
                    "title": str(payload.get("title") or card_id),
                    "url": payload.get("link") or payload.get("source_url") or None,
                    "summary_cn": payload.get("summary") or payload.get("one_liner") or None,
                    "language": payload.get("language") or None,
                    "stars": payload.get("stars"),
                    "type": payload.get("type") or "repo",
                    # 方向与完整概念表：前端据此构造「一张像样的卡片」
                    # 续接学习会话（详情页与会话创建都要用到，不能只有交集）
                    "category": payload.get("category") or "other",
                    "core_concepts": _concepts(payload)[:8],
                    # 对外分数只保留三位小数，且用**原始余弦**而不是加了 bonus 的值：
                    # bonus 是排序手段，不是「更相似」的证据
                    "similarity": round(score, 3),
                    "reason": _reason(shared, category_zh, same_category),
                    "shared_concepts": shared[:4],
                },
            )
        )

    # 稳定排序：并列时保留 Qdrant 给出的相对顺序（它按余弦排过）
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[: max(0, limit)]]


__all__ = ["build_similar_items"]
