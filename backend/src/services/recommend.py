# Services - 推荐排序内核
"""兴趣向量排序的纯函数内核（无 IO，便于离线单测）。

职责边界：
- 入参是「候选卡片（payload + 已存向量）」和「用户画像向量」，出参是排好序的卡片；
- 不连 Qdrant、不连数据库、不调 LLM —— 取向量与读画像由调用方（interest_profile /
  discover 路由）负责，这样排序规则可以被独立验证。

两个必须做对的细节：

1. **退化向量必须过滤**。写入侧在 embedding 失败时会落 `[0.1] * 1536` 这种常量向量，
   它与任何向量的余弦都相同 —— 留着它排序就等于往结果里掺噪声，且看起来「排序生效了」。
2. **类别打散**。纯按相似度排序会把同一类别的卡片堆在一起（例如全是 ai_ml），
   首屏 3 张卡片同质化，体验上比随机还差。这里做「相邻两张尽量不同类」的贪心交错。
"""

from math import sqrt
from statistics import pstdev
from typing import Any, Dict, List, Optional, Sequence

# 退化判定阈值：真实 embedding 各维差异远大于此，只有降级/全零向量才会贴近 0
_DEGENERATE_STD = 1e-6
# 余弦到 [0,1] 的映射下限保护：避免浮点噪声把相似度推到边界外
_EPS = 1e-12

# 推荐理由的分档阈值（余弦相似度）。分档而不是直接抛分数，是因为分数对用户没有意义。
_HIGH_SIM = 0.55
_MID_SIM = 0.40


def is_degenerate_vector(vector: Optional[Sequence[float]]) -> bool:
    """向量是否退化（缺失 / 零长 / 常量 / 全零）：这类向量不携带语义，不能参与排序"""
    if vector is None or len(vector) == 0:
        return True
    try:
        return pstdev(vector) < _DEGENERATE_STD
    except (TypeError, ValueError):
        return True


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """余弦相似度；维度不一致或无有效模长时返回 None（宁可跳过，不算错）"""
    if a is None or b is None or len(a) != len(b) or len(a) == 0:
        return None
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= _EPS or norm_b <= _EPS:
        return None
    return dot / (sqrt(norm_a) * sqrt(norm_b))


def normalize_score(similarity: float) -> float:
    """把 [-1,1] 的余弦映射到 [0,1] 的推荐分（固定映射，跨请求可比）"""
    score = (similarity + 1.0) / 2.0
    return round(min(1.0, max(0.0, score)), 3)


def _reason(similarity: float, payload: Dict[str, Any]) -> str:
    """给出一句可读的中文推荐理由（类别已知时带上类别，便于用户理解）"""
    category = payload.get("category")
    try:
        from src.modules.discovery.summary_spec import CATEGORY_LABELS

        category_zh = CATEGORY_LABELS.get(category)
    except Exception:  # noqa: BLE001 - 理由文案不该反过来炸掉排序
        category_zh = None

    if similarity >= _HIGH_SIM:
        return f"与你近期关注的「{category_zh}」方向高度相关" if category_zh else "与你近期关注的方向高度相关"
    if similarity >= _MID_SIM:
        return f"与你近期关注的「{category_zh}」方向相近" if category_zh else "与你近期关注的方向相近"
    return "与你的兴趣有一定交集"


def diversify_by_category(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """类别打散：相邻两张尽量不同类（同类扎堆时主动往后找一个不同类的顶上）

    贪心实现：每次取队首，但若它与上一张同类、且后面还存在不同类的卡片，就换成
    后面第一张不同类的。找不到（全是同一类）时才允许同类相邻。

    对外的意义：「换一批」在排好序的池子里截取随机窗口后，可以再调一次本函数把
    窗口内顺序打散，避免窗口恰好落成「连续三张同类」。
    """
    pool = list(items)
    result: List[Dict[str, Any]] = []
    last_category = None

    while pool:
        pick = 0
        if last_category is not None and pool[0].get("category") == last_category:
            for index, item in enumerate(pool):
                if item.get("category") != last_category:
                    pick = index
                    break
        chosen = pool.pop(pick)
        result.append(chosen)
        last_category = chosen.get("category")

    return result


def rank_candidates(
    candidates: Sequence[Dict[str, Any]],
    profile: Optional[Sequence[float]],
    k: int,
    *,
    diversify: bool = True,
) -> List[Dict[str, Any]]:
    """按画像相似度排序候选卡片，返回 top-k（每项为 payload + 推荐字段）

    Args:
        candidates: `[{"payload": {...}, "vector": [...]}]`，向量缺失/退化会被过滤
        profile: 用户画像向量（已归一化），None 或退化时直接返回 []（由调用方回退随机）
        k: 返回条数
        diversify: 是否做类别打散

    返回项在 payload 之上附加：
        - `recommend_score`: 0~1 推荐分
        - `recommend_reason`: 一句中文理由
    """
    if k <= 0 or is_degenerate_vector(profile):
        return []

    scored: List[Dict[str, Any]] = []
    for candidate in candidates:
        vector = candidate.get("vector")
        payload = candidate.get("payload") or {}
        if is_degenerate_vector(vector):
            continue
        similarity = cosine_similarity(profile, vector)
        if similarity is None:
            continue
        scored.append(
            {
                "similarity": similarity,
                "payload": payload,
            }
        )

    if not scored:
        return []

    scored.sort(key=lambda item: item["similarity"], reverse=True)

    # 先生成 payload 再打散：打散函数看的是 payload 上的 category，
    # 若直接传 {"similarity", "payload"} 包装层，category 取不到，打散会静默失效。
    ranked: List[Dict[str, Any]] = []
    for item in scored:
        payload = dict(item["payload"])
        payload["recommend_score"] = normalize_score(item["similarity"])
        payload["recommend_reason"] = _reason(item["similarity"], item["payload"])
        ranked.append(payload)

    if diversify:
        ranked = diversify_by_category(ranked)

    return ranked[:k]


__all__ = [
    "cosine_similarity",
    "diversify_by_category",
    "is_degenerate_vector",
    "normalize_score",
    "rank_candidates",
]
