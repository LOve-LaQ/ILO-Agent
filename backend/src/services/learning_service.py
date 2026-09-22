# Services - 学习会话落库
"""学习会话的 PostgreSQL 落库层（阶段 3 行为溯源）。

解决的痛点：Redis 里那份会话上下文 TTL 只有 1 小时，于是
- 对话历史一小时后就蒸发；
- 测验分数 / FSRS 复习计划随会话过期一起丢；
- 「我的学习记录」这种跨设备页面无从实现。

分层不变：**Redis 是热路径缓存，PostgreSQL 是真相源**。本模块只负责真相源读写，
全部 best-effort（永不抛出）且受 `is_database_configured()` 门控 —— 未配置数据库的
纯 demo 环境下，学习链路必须照常可用，只是不留痕。
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Union

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.db import get_session_factory, is_database_configured
from src.models.learning import ChatMessage, LearningSession

UserId = Union[uuid.UUID, str, None]

# 会话状态取值集合（与 LearningSession.state 注释、LearningState 枚举一致）
SESSION_STATES = ("idle", "pushed", "learning", "quiz", "fsrs_update", "completed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_user_id(value: UserId) -> Optional[uuid.UUID]:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def state_value(value: Any) -> str:
    """把状态统一成字符串值。

    `LearningState` 是 `(str, Enum)`，但 Python 3.11 起 `str(成员)` 得到的是
    "LearningState.IDLE" 而非 "idle"，所以必须先取 `.value` 再降级到 `str()`。
    未知取值一律归为 idle，避免脏值写进 `learning_sessions.state`。
    """
    if value is None:
        return "idle"
    raw = getattr(value, "value", value)
    text = str(raw)
    return text if text in SESSION_STATES else "idle"


def parse_datetime(value: Any) -> Optional[datetime]:
    """把 ISO 字符串 / datetime / None 安全转成带时区的 datetime。

    上下文在 Redis 里过了一趟 JSON，datetime 会变成字符串；直接和
    `datetime.now(timezone.utc)` 相减会抛 TypeError，因此统一在这里收口。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)



# ==================== 写入 ====================


def upsert_session(
    session_id: str,
    *,
    user_id: UserId,
    card_id: Optional[str] = None,
    topic: str,
    summary: Optional[str] = None,
    core_concepts: Optional[Sequence[str]] = None,
    time_budget_minutes: int = 15,
    preferred_depth: str = "medium",
    state: Any = "idle",
    started_at: Optional[datetime] = None,
) -> None:
    """创建会话记录（重复调用则更新卡片侧字段，不改动进度字段）"""
    if not is_database_configured():
        return
    uid = _coerce_user_id(user_id)
    if uid is None:
        logger.warning("[WARN] 会话落库缺少有效 user_id，跳过")
        return
    try:
        with get_session_factory()() as session:
            stmt = pg_insert(LearningSession).values(
                session_id=session_id,
                user_id=uid,
                card_id=card_id,
                topic=(topic or "")[:255],
                summary=summary,
                core_concepts=list(core_concepts or []),
                time_budget_minutes=time_budget_minutes,
                preferred_depth=preferred_depth,
                state=state_value(state),
                started_at=started_at or _now(),
            )
            # 只覆盖卡片侧字段：测验分数 / 完成时间等进度字段交给
            # sync_state_from_context / complete_session_record 单独维护，
            # 避免一次重复创建把已经做完的会话打回 idle。
            stmt = stmt.on_conflict_do_update(
                index_elements=["session_id"],
                set_={
                    "card_id": stmt.excluded.card_id,
                    "topic": stmt.excluded.topic,
                    "summary": stmt.excluded.summary,
                    "core_concepts": stmt.excluded.core_concepts,
                    "time_budget_minutes": stmt.excluded.time_budget_minutes,
                    "preferred_depth": stmt.excluded.preferred_depth,
                },
            )
            session.execute(stmt)
            session.commit()
    except Exception as e:  # noqa: BLE001 - 落库失败不该把用户的学习流程带崩
        logger.warning(f"[WARN] 会话落库失败（不影响学习流程）: {session_id} - {e}")


def update_session(session_id: str, **fields: Any) -> None:
    """按需更新会话字段（白名单外的键忽略，防止拼错字段名静默写入）"""
    if not is_database_configured():
        return
    allowed = {
        "state",
        "quiz_questions",
        "quiz_score",
        "fsrs_rating",
        "fsrs_next_review_at",
        "completed_at",
    }
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        return
    try:
        with get_session_factory()() as session:
            record = session.execute(
                select(LearningSession).where(LearningSession.session_id == session_id)
            ).scalar_one_or_none()
            if record is None:
                return
            for key, value in payload.items():
                if key == "state":
                    value = state_value(value)
                elif key == "fsrs_next_review_at":
                    value = parse_datetime(value)
                setattr(record, key, value)
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 会话更新失败: {session_id} - {e}")


def sync_state_from_context(context: Dict[str, Any], state: Any = None) -> None:
    """把状态机上下文里的进度镜像回数据库（状态 + 测验题 + 分数 + 复习计划）

    `state` 可显式覆盖：状态机有几个分支只改返回值不改 `current_state`
    （如 `start_learning` 内部记 learning、返回 quiz），以调用方拿到的
    `result["state"]` 为准才不会把 DB 里的状态写偏。

    **只同步上下文里确实存在的字段**：上下文里没有 ≠ 要求清空。否则 /push
    这类不带测验信息的调用会把已落库的 quiz_questions 抹成 NULL。
    """
    session_id = context.get("session_id")
    if not session_id:
        return

    payload: Dict[str, Any] = {"state": state if state is not None else context.get("current_state")}
    for field in ("quiz_questions", "quiz_score", "fsrs_rating"):
        if context.get(field) is not None:
            payload[field] = context[field]
    if context.get("next_review_date") is not None:
        payload["fsrs_next_review_at"] = context["next_review_date"]

    update_session(session_id, **payload)



def append_messages(session_id: str, messages: Sequence[Dict[str, Any]]) -> None:
    """追加若干条对话（一条 INSERT，不做逐条往返）

    显式给每条消息写 `created_at`，而不是依赖列上的 `server_default=now()`：
    `now()` 取的是**事务时间戳**，同一事务里插入的 user + assistant 会拿到完全相同的值。
    两处后果 ——
    - `load_messages` 按 `created_at` 升序取回时这两条的先后**无从确定**，回看的对话
      顺序可能颠倒；
    - 客户端拿时间戳当列表 key，会撞 key 丢消息（React 的重复 key 警告即由此而来）。

    这里以调用时刻为基准按毫秒递增，保证同一批内严格有序且唯一；不同轮之间隔着一次
    模型调用，基准时间天然拉开，因此跨批顺序也稳定。
    """
    if not is_database_configured() or not session_id:
        return
    base = _now()
    rows = [
        ChatMessage(
            session_id=session_id,
            role=str(m.get("role") or "user")[:16],
            content=str(m.get("content") or ""),
            created_at=base + timedelta(milliseconds=index),
        )
        for index, m in enumerate(messages)
        if (m or {}).get("content")
    ]
    if not rows:
        return
    try:
        with get_session_factory()() as session:
            session.add_all(rows)
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 对话落库失败（不影响回答）: {session_id} - {e}")


def complete_session_record(
    session_id: str,
    *,
    quiz_score: Optional[float] = None,
    fsrs_rating: Optional[int] = None,
    next_review_at: Any = None,
) -> None:
    """标记会话完成：终态 + 分数 + 复习计划 + 完成时间"""
    if not is_database_configured():
        return
    try:
        with get_session_factory()() as session:
            record = session.execute(
                select(LearningSession).where(LearningSession.session_id == session_id)
            ).scalar_one_or_none()
            if record is None:
                return
            record.state = "completed"
            record.quiz_score = quiz_score if quiz_score is not None else record.quiz_score
            record.fsrs_rating = fsrs_rating if fsrs_rating is not None else record.fsrs_rating
            record.fsrs_next_review_at = parse_datetime(next_review_at) or record.fsrs_next_review_at
            record.completed_at = _now()
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 会话完成落库失败: {session_id} - {e}")


# ==================== 读取 ====================


def load_session(session_id: str) -> Optional[Dict[str, Any]]:
    """从数据库回读会话，组装成状态机可直接使用的上下文。

    字段名刻意与 `LearningStateMachine.create_session` 产出的上下文对齐
    （`news_item_id` / `start_time` / `current_state`），这样「内存 → Redis →
    PostgreSQL」三级回退拿到的都是同一种形状，上层无需分支判断。
    """
    if not is_database_configured() or not session_id:
        return None
    try:
        with get_session_factory()() as session:
            record = session.execute(
                select(LearningSession).where(LearningSession.session_id == session_id)
            ).scalar_one_or_none()
            if record is None:
                return None
            context: Dict[str, Any] = {
                "session_id": record.session_id,
                # 上游统一用字符串做归属比对，这里必须同型，否则 UUID != str 恒真 → 误判 403
                "user_id": str(record.user_id),
                "news_item_id": record.card_id,
                "topic": record.topic,
                "summary": record.summary or "",
                "core_concepts": list(record.core_concepts or []),
                "time_budget_minutes": record.time_budget_minutes,
                "preferred_depth": record.preferred_depth,
                "start_time": parse_datetime(record.started_at) or _now(),
                "current_state": record.state,
                "source": "postgres",
            }
            if record.quiz_questions:
                context["quiz_questions"] = list(record.quiz_questions)
            if record.quiz_score is not None:
                context["quiz_score"] = record.quiz_score
            if record.fsrs_rating is not None:
                context["fsrs_rating"] = record.fsrs_rating
            if record.fsrs_next_review_at is not None:
                context["next_review_date"] = record.fsrs_next_review_at.isoformat()
            return context
    except Exception as e:  # noqa: BLE001 - 回读失败就当没有，由上层抛 SESSION_NOT_FOUND
        logger.warning(f"[WARN] 会话回读失败: {session_id} - {e}")
        return None


def list_messages(
    session_id: str, limit: int = 200, offset: int = 0
) -> List[Dict[str, Any]]:
    """按时间序分页取会话对话（供「会话历史」接口用）

    【为什么必须显式分页】此前只有一个内部写死的 200 条上限，超出的对话被
    **静默丢弃**：详情接口还把截断后的条数当成 message_count 报出去，于是长会话
    回看时看不出「后面还有」。现在 offset/limit 由调用方给出，总量与是否还有下一页
    由接口一并返回（见 count_messages），截断不再无声。

    排序用 `created_at, id` 双键：一轮问答的两条消息时间戳可能落在同一微秒，
    只按时间排会得到不稳定的顺序，翻页时可能出现重复或漏读。
    """
    if not is_database_configured() or not session_id:
        return []
    try:
        with get_session_factory()() as session:
            rows = session.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                .offset(max(0, offset))
                .limit(max(1, limit))
            ).scalars()
            return [
                {"role": row.role, "content": row.content, "created_at": row.created_at}
                for row in rows
            ]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 会话对话读取失败: {session_id} - {e}")
        return []


def count_messages(session_id: str) -> int:
    """单个会话的对话总条数（分页契约的一部分；失败返回 0）"""
    if not is_database_configured() or not session_id:
        return 0
    try:
        with get_session_factory()() as session:
            return int(
                session.execute(
                    select(func.count(ChatMessage.id)).where(
                        ChatMessage.session_id == session_id
                    )
                ).scalar_one()
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 会话对话总数统计失败: {session_id} - {e}")
        return 0


def _session_brief(record: LearningSession) -> Dict[str, Any]:
    """列表/详情共用的会话摘要（不含对话正文，避免列表接口把响应拖大）"""
    return {
        "session_id": record.session_id,
        "card_id": record.card_id,
        "topic": record.topic,
        "summary": record.summary,
        "state": record.state,
        "quiz_score": record.quiz_score,
        "fsrs_rating": record.fsrs_rating,
        "fsrs_next_review_at": record.fsrs_next_review_at,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
    }


def list_sessions(
    user_id: UserId, limit: int = 20, offset: int = 0, state: Optional[str] = None
) -> List[Dict[str, Any]]:
    """按开始时间倒序取某个用户的学习会话（只按 user_id 过滤，天然隔离他人数据）

    state 过滤下推到 SQL：若「先分页再在内存里过滤」，第二页会返回错乱的条数。
    """
    uid = _coerce_user_id(user_id)
    if not is_database_configured() or uid is None:
        return []
    try:
        with get_session_factory()() as session:
            stmt = (
                select(LearningSession)
                .where(LearningSession.user_id == uid)
                .order_by(LearningSession.started_at.desc())
                .limit(max(1, min(limit, 100)))
                .offset(max(0, offset))
            )
            if state:
                stmt = stmt.where(LearningSession.state == state)
            return [_session_brief(row) for row in session.execute(stmt).scalars()]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 学习会话列表读取失败: {e}")
        return []


def get_session(session_id: str, user_id: UserId) -> Optional[Dict[str, Any]]:
    """取单个会话（带归属校验；返回 None 表示不存在或不属于该用户）"""
    uid = _coerce_user_id(user_id)
    if not is_database_configured() or uid is None or not session_id:
        return None
    try:
        with get_session_factory()() as session:
            record = session.execute(
                select(LearningSession).where(
                    LearningSession.session_id == session_id,
                    # 归属条件直接写进 SQL：越权请求连记录都取不到，
                    # 不必依赖调用方记得再做一次比对
                    LearningSession.user_id == uid,
                )
            ).scalar_one_or_none()
            return _session_brief(record) if record is not None else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 学习会话读取失败: {session_id} - {e}")
        return None


def count_messages_by_session(session_ids: Sequence[str]) -> Dict[str, int]:
    """批量统计每个会话的对话条数（一次 group by，避免列表页 N+1）"""
    ids = [s for s in session_ids if s]
    if not is_database_configured() or not ids:
        return {}
    try:
        with get_session_factory()() as session:
            rows = session.execute(
                select(ChatMessage.session_id, func.count(ChatMessage.id))
                .where(ChatMessage.session_id.in_(ids))
                .group_by(ChatMessage.session_id)
            )
            return {row[0]: row[1] for row in rows}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 对话条数统计失败: {e}")
        return {}


def session_stats(user_id: UserId) -> Dict[str, Any]:
    """个人中心用的聚合统计（一条 SQL 出全部数字）"""
    uid = _coerce_user_id(user_id)
    empty: Dict[str, Any] = {
        "sessions_total": 0,
        "sessions_completed": 0,
        "average_score": None,
    }
    if not is_database_configured() or uid is None:
        return empty
    try:
        with get_session_factory()() as session:
            row = session.execute(
                select(
                    func.count(LearningSession.id),
                    func.count(LearningSession.id).filter(
                        LearningSession.state == "completed"
                    ),
                    func.avg(LearningSession.quiz_score),
                ).where(LearningSession.user_id == uid)
            ).one()
            return {
                "sessions_total": row[0] or 0,
                "sessions_completed": row[1] or 0,
                "average_score": round(float(row[2]), 4) if row[2] is not None else None,
            }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 学习统计失败: {e}")
        return empty



__all__ = [
    "SESSION_STATES",
    "append_messages",
    "complete_session_record",
    "count_messages",
    "count_messages_by_session",
    "get_session",
    "list_messages",
    "list_sessions",
    "load_session",
    "parse_datetime",
    "session_stats",
    "state_value",
    "sync_state_from_context",
    "update_session",
    "upsert_session",
]
