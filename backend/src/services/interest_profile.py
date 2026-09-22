# Services - 兴趣画像
"""用户兴趣画像：把「学习会话 + 收藏 + 行为流水」压成一个兴趣向量。

设计要点：

- **向量来源是卡片在 Qdrant 的已存向量**，不是拿 summary 重新 embed。重新 embed 既多付
  一次成本，又可能与入库向量不在同一语义空间（换过 embedding 模型），算出来的相似度
  没有意义。
- **加权平均 + 时间指数衰减**：`0.5 ** (age_days / 半衰期)`。恒久不变地累计历史行为，
  会让用户两年前关注的 Rust 永远压过这周的 AI —— 那不是「兴趣」，是档案。
- **指纹失效**：`embedding_model` 变了、参与构建的卡片数变了、或记录缺失 → 重算。
  指纹是「语义空间 + 维度」的标识，变了就必须重算而不是复用（错配比没有更难排查）。
- **全程 best-effort**：数据库/知识库不可用、无有效向量都返回 None，由上层回退随机。
  宁可「这次不个性化」，也不能让首页 500 或假装个性化生效。
"""

import os
import uuid
from datetime import datetime, timezone
from math import sqrt
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from loguru import logger
from sqlalchemy import select

from src.core.config import settings
from src.core.db import get_session_factory, is_database_configured
from src.models.engagement import UserActivity, UserBookmark
from src.models.interest import UserInterestProfile
from src.models.learning import LearningSession
from src.services.recommend import is_degenerate_vector

UserId = Union[uuid.UUID, str, None]

# 行为流水里只有 target_type='card' 的动作，其 target_id 才是卡片 id；
# 会话类动作（start_session / submit_quiz / complete_session / ask_question ...）
# 的 target_type 是 'session'，target_id 是 sess_xxx 这类会话 id。
# 早期版本只按 action_type 过滤，结果把会话 id 当卡片 id 塞进了候选池：它们既取不到
# 向量（白跑一轮查询），又会在权重表里与真实卡片抢 top-N 名额，还会让
# source_card_count 这类计数失真。会话生命周期信号一律只从 learning_sessions 表取。
_CARD_TARGET = "card"

# 信号权重：依据「用户意图的明确程度」，而不是「动作的稀有度」。
# bookmark 是显式表达，view_card 只是曝光的副产品，权重最低。
_CARD_SIGNAL_WEIGHTS: Dict[str, float] = {
    "bookmark": 3.0,
    "view_card": 1.0,
}

# 围绕卡片发起一次学习：权重落在收藏与浏览之间。
# 会话的完成/测验严格来说是更强的信号，但流水表里拿不到卡片 id，按状态细分的
# 收益远小于额外查询的成本，故统一按此权重计。
_SESSION_WEIGHT = 2.5

# 单次画像构建最多读多少条信号：重度用户的流水可能上万条，不设上限会把一次
# 冷启动变成秒级查询。取最近的若干条即可 —— 衰减本身就让旧行为权重趋近于零。
_MAX_SIGNAL_ROWS = 500

# 权重为正即可参与；防止未来接入负反馈（如「不感兴趣」）时把向量算歪
_MIN_WEIGHT = 0.0


def _coerce_user_id(value: UserId) -> Optional[uuid.UUID]:
    """容忍 str / UUID / None（路由传进来的可能是字符串）"""
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _age_days(occurred_at: Optional[datetime], now: datetime) -> float:
    """行为距今天数；时间为空按 0 天算（不给它额外加权，但也不因缺失而丢弃）"""
    if occurred_at is None:
        return 0.0
    if occurred_at.tzinfo is None:
        # timestamptz 落库后通常是带时区的；naive 值一律按 UTC 解释，
        # 否则与 aware 的 now 相减会直接 TypeError
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    return max(0.0, (now - occurred_at).total_seconds() / 86400.0)


def aggregate_signals(
    signals: Iterable[Tuple[str, float, Optional[datetime]]],
    *,
    now: Optional[datetime] = None,
    half_life_days: Optional[int] = None,
) -> Dict[str, float]:
    """把 `(卡片 id, 基础权重, 发生时间)` 聚合成 `{卡片 id: 衰减后总权重}`（纯函数）

    半衰期配 0 或负数时退化为「不做衰减」：这是明确可用的配置（关掉时间维度），
    而不是错误，所以不抛异常。
    """
    now = now or datetime.now(timezone.utc)
    half_life = (
        half_life_days
        if half_life_days is not None
        else settings.interest_profile_half_life_days
    )

    weights: Dict[str, float] = {}
    for item_id, weight, occurred_at in signals:
        if not item_id:
            continue
        factor = 1.0
        if half_life and half_life > 0:
            factor = 0.5 ** (_age_days(occurred_at, now) / half_life)
        weights[item_id] = weights.get(item_id, 0.0) + float(weight) * factor
    return weights


def top_candidates(weights: Dict[str, float], limit: int) -> List[str]:
    """按权重取 top-N 卡片 id（权重相同时按 id 排序，保证结果可复现）"""
    if limit <= 0:
        return []
    ordered = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))
    return [item_id for item_id, _ in ordered[:limit]]


def weighted_mean_vector(
    pairs: Iterable[Tuple[float, Sequence[float]]],
) -> Optional[List[float]]:
    """按权重求向量加权平均，再 L2 归一化（纯函数）

    - 退化向量（常量/全零/缺失）直接剔除：它们与任何方向的余弦都相同，留下只会掺噪声；
    - 维度与首个可用向量不一致的跳过（说明混进了另一个语义空间的向量）；
    - 无任何可用向量时返回 None，由上层回退随机。
    """
    usable: List[Tuple[float, List[float]]] = []
    for weight, vector in pairs:
        if weight <= _MIN_WEIGHT or is_degenerate_vector(vector):
            continue
        usable.append((float(weight), [float(x) for x in vector]))

    if not usable:
        return None

    dim = len(usable[0][1])
    acc = [0.0] * dim
    total = 0.0
    for weight, vector in usable:
        if len(vector) != dim:
            continue
        for index, value in enumerate(vector):
            acc[index] += weight * value
        total += weight

    if total <= 0:
        return None

    acc = [value / total for value in acc]
    norm = sqrt(sum(value * value for value in acc))
    if norm <= 1e-12:
        return None
    return [value / norm for value in acc]


def embedding_fingerprint() -> Optional[str]:
    """当前 embedding 的语义空间指纹（`provider:model:维度`）

    直接读 EmbeddingService 单例上的 provider / model，而不是在这里另抄一份模型名 ——
    指纹的价值全在「模型一换它就变」，抄一份就必然会漂。单例已被采集链路创建，
    这里不会新建 HTTP 客户端。取不到时返回 None：没有确定的语义空间就不构建画像。
    """
    api_key = (
        os.getenv("ALIYUN_API_KEY")
        or os.getenv("VOYAGE_API_KEY")
        or os.getenv("ZHIPU_API_KEY")
        or ""
    )
    if not api_key:
        return None
    try:
        from src.modules.agent.embedding_service import get_embedding_service
        from src.modules.discovery.tech_knowledge import VECTOR_SIZE

        service = get_embedding_service(api_key)
    except Exception as e:  # noqa: BLE001 - 指纹取不到就当作「不入画像」
        logger.warning(f"[WARN] 无法确定 embedding 指纹: {e}")
        return None

    provider = getattr(service, "_current_provider", "unknown")
    model = getattr(service, f"{provider}_model", "unknown")
    return f"{provider}:{model}:{VECTOR_SIZE}"


def collect_signals(uid: uuid.UUID) -> List[Tuple[str, float, Optional[datetime]]]:
    """从三处取原始信号：收藏、学习会话、卡片行为流水（一次画像构建只读一轮）"""
    actions = tuple(_CARD_SIGNAL_WEIGHTS)
    signals: List[Tuple[str, float, Optional[datetime]]] = []

    with get_session_factory()() as session:
        rows = session.execute(
            select(UserBookmark.item_id, UserBookmark.created_at)
            .where(UserBookmark.user_id == uid)
            .order_by(UserBookmark.created_at.desc())
            .limit(_MAX_SIGNAL_ROWS)
        ).all()
        signals.extend(
            (row[0], _CARD_SIGNAL_WEIGHTS["bookmark"], row[1]) for row in rows if row[0]
        )

        rows = session.execute(
            select(LearningSession.card_id, LearningSession.started_at)
            .where(
                LearningSession.user_id == uid,
                LearningSession.card_id.is_not(None),
            )
            .order_by(LearningSession.started_at.desc())
            .limit(_MAX_SIGNAL_ROWS)
        ).all()
        signals.extend(
            (row[0], _SESSION_WEIGHT, row[1]) for row in rows if row[0]
        )

        rows = session.execute(
            select(
                UserActivity.target_id,
                UserActivity.action_type,
                UserActivity.created_at,
            )
            .where(
                UserActivity.user_id == uid,
                UserActivity.target_type == _CARD_TARGET,
                UserActivity.action_type.in_(actions),
                UserActivity.target_id.is_not(None),
            )
            .order_by(UserActivity.created_at.desc())
            .limit(_MAX_SIGNAL_ROWS)
        ).all()
        signals.extend(
            (row[0], _CARD_SIGNAL_WEIGHTS[row[1]], row[2])
            for row in rows
            if row[0] and row[1] in _CARD_SIGNAL_WEIGHTS
        )

    return signals


def _load_stored(uid: uuid.UUID) -> Optional[Tuple[List[float], str, int]]:
    """读已存画像，返回 `(vector, embedding_model, source_card_count)`；读不到返回 None"""
    try:
        with get_session_factory()() as session:
            row = session.get(UserInterestProfile, uid)
            if row is None:
                return None
            return (
                [float(x) for x in (row.vector or [])],
                str(row.embedding_model or ""),
                int(row.source_card_count or 0),
            )
    except Exception as e:  # noqa: BLE001 - 读失败退化为「没有画像」，仍可重算
        logger.warning(f"[WARN] 兴趣画像读取失败: {e}")
        return None


def _save_profile(
    uid: uuid.UUID, vector: List[float], fingerprint: str, card_count: int
) -> None:
    """写回画像（best-effort）：写失败只影响下次是否重算，不能影响本次推荐"""
    try:
        with get_session_factory()() as session:
            row = session.get(UserInterestProfile, uid)
            if row is None:
                row = UserInterestProfile(user_id=uid)
                session.add(row)
            row.vector = vector
            row.embedding_model = fingerprint
            row.source_card_count = card_count
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 兴趣画像写入失败（不影响本次推荐）: {e}")


def build_user_profile(user_id: UserId) -> Optional[List[float]]:
    """构建（或复用）用户兴趣向量；任一环节不可用返回 None，上层据此回退随机。

    复用条件（三者同时成立才复用，否则重算）：
    1. 已存画像存在；
    2. `embedding_model` 指纹与当前一致（语义空间没变）；
    3. 参与构建的卡片数一致（没有新的兴趣信号进来）。
    """
    uid = _coerce_user_id(user_id)
    if uid is None or not is_database_configured():
        return None

    fingerprint = embedding_fingerprint()
    if fingerprint is None:
        return None

    try:
        signals = collect_signals(uid)
    except Exception as e:  # noqa: BLE001 - 取信号失败即「这次不个性化」
        logger.warning(f"[WARN] 兴趣信号读取失败: {e}")
        return None

    weights = aggregate_signals(signals)
    candidates = top_candidates(weights, settings.interest_profile_max_candidates)
    if not candidates:
        return None  # 新用户 / 无行为：没有画像可构建，交给随机兜底

    stored = _load_stored(uid)
    if (
        stored is not None
        and stored[1] == fingerprint
        and stored[2] == len(candidates)
        and not is_degenerate_vector(stored[0])
    ):
        return stored[0]

    try:
        from src.modules.discovery.tech_knowledge import get_knowledge_base

        vectors = get_knowledge_base().get_vectors_by_ids(candidates)
    except Exception as e:  # noqa: BLE001 - 知识库不可用 → 回退随机
        logger.warning(f"[WARN] 画像取向量失败: {e}")
        return None

    profile = weighted_mean_vector(
        (weights[item_id], vectors[item_id])
        for item_id in candidates
        if item_id in vectors
    )
    if profile is None:
        # 候选全退化/无向量：不写入画像（写一个空画像反而会让下次误判为「已有画像」）
        return None

    _save_profile(uid, profile, fingerprint, len(candidates))
    return profile


__all__ = [
    "aggregate_signals",
    "build_user_profile",
    "collect_signals",
    "embedding_fingerprint",
    "top_candidates",
    "weighted_mean_vector",
]
