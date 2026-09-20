# Services - 采集溯源
"""采集链路统一入口（阶段 3 内容溯源）。

把「抓取 → 去重 → 摘要 → 入库 → 记录」收敛到一个编排里，让手动刷新与定时任务
共用同一条可追溯路径：

    start_batch()   -> collection_batches 落一条 running 批次
    collect_items() -> 去重 -> 摘要 -> store() -> collection_records 落记录
    finish_batch()  -> 回填计数与终态

去重的**真相源**是 `collection_records.item_id`（唯一约束），Redis 的
`crawled:item_ids` 降级为加速缓存并保持双写；未配置数据库时自动退化为
「只用 Redis」，保证无库环境下 demo 仍可用。
"""

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Union

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.db import get_session_factory, is_database_configured
from src.models.collection import CollectionBatch, CollectionRecord

BATCH_TRIGGER_MANUAL = "manual"
BATCH_TRIGGER_SCHEDULED = "scheduled"

UserId = Union[uuid.UUID, str, None]

# 卡片 `source`（展示名）-> 溯源用的平台标识。
# 仅用于没有显式 `source_platform` 的历史卡片；新采集由 fetcher 直接给出。
PLATFORM_BY_SOURCE: Dict[str, str] = {
    "GitHub Trending": "github",
    "Hacker News": "hackernews",
    "Lobsters": "lobsters",
    "dev.to": "devto",
    "Stack Overflow": "stackoverflow",
}

_redis_client = None


def get_redis_client():
    """采集侧 Redis 连接（与 tech_knowledge 共用同一个已抓取集合）"""
    global _redis_client
    if _redis_client is None:
        import redis as redis_lib

        _redis_client = redis_lib.Redis.from_url(
            os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
        )
    return _redis_client


# ==================== Redis（加速缓存，双写） ====================


def _crawled_set() -> str:
    """集合名以 tech_knowledge 为唯一来源，避免两处常量漂移"""
    from src.modules.discovery.tech_knowledge import CRAWLED_SET

    return CRAWLED_SET


def _redis_mark(item_ids: Sequence[str]) -> None:
    """把已采集的 id 镜像进 Redis（失败只告警：DB 才是真相源）"""
    ids = [str(i) for i in item_ids if i]
    if not ids:
        return
    try:
        get_redis_client().sadd(_crawled_set(), *ids)
    except Exception as e:  # noqa: BLE001 - 缓存失败不影响采集
        logger.warning(f"[WARN] Redis 标记已采集失败（不影响主流程）: {e}")


def _redis_collected(item_ids: Sequence[str]) -> Set[str]:
    """从 Redis 反查已采集的 id（逐条 sismember，量级为单次抓取条数）"""
    hits: Set[str] = set()
    try:
        client = get_redis_client()
        name = _crawled_set()
        for item_id in item_ids:
            if client.sismember(name, str(item_id)):
                hits.add(str(item_id))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 读取 Redis 去重集合失败: {e}")
    return hits


# ==================== PostgreSQL（真相源） ====================


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_user_id(value: UserId) -> Optional[uuid.UUID]:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _sid(value: Optional[uuid.UUID]) -> Optional[str]:
    return str(value) if value is not None else None


def start_batch(
    kind: str,
    *,
    trigger: str = BATCH_TRIGGER_MANUAL,
    operator_user_id: UserId = None,
    params: Optional[Dict[str, Any]] = None,
) -> Optional[uuid.UUID]:
    """开一条 running 批次；未配置数据库时返回 None（调用方按「无溯源」继续）"""
    if not is_database_configured():
        logger.warning("[WARN] 未配置数据库，本次采集不落溯源批次")
        return None
    try:
        with get_session_factory()() as session:
            batch = CollectionBatch(
                kind=kind,
                trigger=trigger,
                operator_user_id=_coerce_user_id(operator_user_id),
                params=params,
                status="running",
            )
            session.add(batch)
            session.commit()
            return batch.id
    except Exception as e:  # noqa: BLE001 - 溯源失败不该阻断采集
        logger.warning(f"[WARN] 创建采集批次失败（继续采集）: {e}")
        return None


def finish_batch(
    batch_id: Optional[uuid.UUID],
    *,
    status: str,
    fetched_count: int = 0,
    new_count: int = 0,
    skipped_count: int = 0,
    failed_count: int = 0,
    error: Optional[str] = None,
) -> None:
    """回填批次终态与计数"""
    if batch_id is None or not is_database_configured():
        return
    try:
        with get_session_factory()() as session:
            batch = session.get(CollectionBatch, batch_id)
            if batch is None:
                return
            batch.status = status
            batch.fetched_count = fetched_count
            batch.new_count = new_count
            batch.skipped_count = skipped_count
            batch.failed_count = failed_count
            batch.error = error
            batch.finished_at = _now()
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 回填采集批次失败: {e}")


def get_collected_ids(item_ids: Sequence[str]) -> Set[str]:
    """批量查「已带摘要进过知识库」的 item_id

    只认 `status='summarized'` 的记录：`failed` 表示摘要没成（或没进成知识库），
    下次抓取必须重试 —— 否则一次 LLM 抖动就会让卡片永久停在没摘要的状态。

    DB 与 Redis 取**并集**：Redis 是镜像，但历史回填尚未跑完时它可能比 DB 更全，
    此时若只信 DB 会把老卡片当新卡片重抓重摘要（白花 LLM 成本）。
    """
    wanted = {str(i) for i in item_ids if i}
    if not wanted:
        return set()

    collected: Set[str] = set()
    if is_database_configured():
        try:
            with get_session_factory()() as session:
                rows = (
                    session.execute(
                        select(CollectionRecord.item_id).where(
                            CollectionRecord.item_id.in_(wanted),
                            CollectionRecord.status == "summarized",
                        )
                    )
                    .scalars()
                    .all()
                )
            collected |= set(rows)
        except Exception as e:  # noqa: BLE001 - 查库失败退化为 Redis 去重
            logger.warning(f"[WARN] 查询采集记录失败，退化为 Redis 去重: {e}")

    return collected | _redis_collected(wanted)


def is_collected(item_id: str) -> bool:
    """单个条目是否已采集过"""
    return bool(get_collected_ids([item_id]))


def get_provenance(item_id: str) -> Optional[Dict[str, Any]]:
    """取一张卡片的完整内容溯源（记录 + 所属批次），供 /discover/cards/{id}/provenance 用。

    返回 None 表示数据库里没有这张卡片的采集记录 —— 可能是历史卡片（待回填）
    或内置示例数据，由路由层决定怎么表述，服务层不做「假装有」的兜底。
    """
    if not is_database_configured() or not item_id:
        return None
    try:
        with get_session_factory()() as session:
            record = session.execute(
                select(CollectionRecord).where(CollectionRecord.item_id == item_id)
            ).scalar_one_or_none()
            if record is None:
                return None

            batch = None
            if record.batch_id is not None:
                row = session.get(CollectionBatch, record.batch_id)
                if row is not None:
                    batch = {
                        "id": str(row.id),
                        "kind": row.kind,
                        "trigger": row.trigger,
                        "status": row.status,
                        "params": row.params,
                        "fetched_count": row.fetched_count,
                        "new_count": row.new_count,
                        "skipped_count": row.skipped_count,
                        "failed_count": row.failed_count,
                        "error": row.error,
                        "started_at": row.started_at,
                        "finished_at": row.finished_at,
                    }

            return {
                "item_id": record.item_id,
                "source_platform": record.source_platform,
                "source_url": record.source_url,
                "raw_description": record.raw_description,
                "collected_at": record.collected_at,
                "summary_generated_at": record.summary_generated_at,
                "status": record.status,
                "backfilled": record.backfilled,
                "card_payload": record.card_payload,
                "batch": batch,
            }
    except Exception as e:  # noqa: BLE001 - 溯源查询失败不该让页面炸掉
        logger.warning(f"[WARN] 读取卡片溯源失败: {item_id} - {e}")
        return None


def build_provenance(item: Dict[str, Any], *, status: str = "summarized") -> Dict[str, Any]:
    """从卡片抽出溯源字段（兼容没有显式 source_platform 的历史卡片）"""
    platform = item.get("source_platform")
    if not platform:
        source = item.get("source") or ""
        platform = PLATFORM_BY_SOURCE.get(source, source or None)
    return {
        "source_platform": platform,
        "source_url": item.get("source_url") or item.get("link"),
        # raw_description 缺失时退回 summary：对老卡片是近似值，由 backfilled 标记诚实标注
        "raw_description": item.get("raw_description") or item.get("summary"),
        "status": status,
    }


def record_item(
    batch_id: Optional[uuid.UUID],
    item: Dict[str, Any],
    *,
    status: str = "summarized",
    backfilled: bool = False,
) -> None:
    """写入/更新一条采集记录

    `item_id` 唯一，因此 force 重摘要走 ON CONFLICT 覆盖：
    - `collected_at` 保留首次采集时间（不改），`summary_generated_at` 刷新；
    - `raw_description` 只在原值为空时补齐，避免被 LLM 改写后的 summary 污染。
    """
    if not is_database_configured():
        return
    item_id = str(item.get("id") or "").strip()
    if not item_id:
        return

    prov = build_provenance(item, status=status)
    if len(item_id) > 64:
        logger.warning(f"[WARN] item_id 过长，跳过溯源记录: {item_id[:80]}")
        return

    try:
        with get_session_factory()() as session:
            stmt = pg_insert(CollectionRecord).values(
                id=uuid.uuid4(),
                item_id=item_id,
                batch_id=batch_id,
                source_platform=prov["source_platform"],
                source_url=prov["source_url"],
                raw_description=prov["raw_description"],
                collected_at=_now(),
                summary_generated_at=_now() if status == "summarized" else None,
                card_payload=item,
                status=prov["status"],
                backfilled=backfilled,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["item_id"],
                set_={
                    "batch_id": stmt.excluded.batch_id,
                    "source_platform": stmt.excluded.source_platform,
                    "source_url": stmt.excluded.source_url,
                    "raw_description": func.coalesce(
                        CollectionRecord.raw_description, stmt.excluded.raw_description
                    ),
                    "summary_generated_at": stmt.excluded.summary_generated_at,
                    "card_payload": stmt.excluded.card_payload,
                    "status": stmt.excluded.status,
                },
            )
            session.execute(stmt)
            session.commit()
    except Exception as e:  # noqa: BLE001 - 溯源失败不该阻断采集
        logger.warning(f"[WARN] 写入采集记录失败（不影响主流程）: {item_id} - {e}")


def _approx_collected_at(item: Dict[str, Any]) -> datetime:
    """历史卡片没有真实采集时间，退而取内容发布时间（近似）。

    `collected_at` 是非空列，而各平台的日期格式并不统一：解析失败时宁可留
    「当前时间」这个近似值，也不要让整批回填中途失败 —— 这批数据的
    `backfilled=True` 已经明确标注了「时间是近似值」。
    """
    raw = item.get("published_at") or item.get("created_at")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return _now()


def backfill_records(
    items: Sequence[Dict[str, Any]], *, dry_run: bool = False
) -> Dict[str, int]:
    """为历史卡片补写溯源记录（阶段 3 之前采集的卡片在库里没有记录）。

    不直接复用 `record_item`，因为回填的语义有三点不同：
    - **不覆盖**已有记录：真实采集信息永远比回填的近似值可信；
    - `collected_at` 取内容发布时间，而不是「回填那一刻」—— 否则整批历史卡片
      会被标成同一天采集，时间线一眼假；
    - `batch_id=None` 且 `backfilled=True`：没有真实批次就不编造批次。

    幂等：重复执行只会累加 skipped。返回 {scanned, backfilled, skipped, oversized}。
    """
    if not is_database_configured():
        raise RuntimeError(
            "未配置 DATABASE_URL，无法回填溯源记录。请在 backend/.env 中设置（格式见 .env.example）。"
        )

    seen: Dict[str, Dict[str, Any]] = {}
    oversized = 0
    for item in items:
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue
        if len(item_id) > 64:  # 与 collection_records.item_id 的列宽一致
            oversized += 1
            continue
        seen.setdefault(item_id, item)

    stats = {"scanned": len(seen), "backfilled": 0, "skipped": 0, "oversized": oversized}
    if not seen:
        return stats

    with get_session_factory()() as session:
        existing = set(
            session.execute(
                select(CollectionRecord.item_id).where(
                    CollectionRecord.item_id.in_(list(seen))
                )
            )
            .scalars()
            .all()
        )
        stats["skipped"] = len(existing)

        pending = [(item_id, item) for item_id, item in seen.items() if item_id not in existing]
        if dry_run or not pending:
            return stats

        rows: List[Dict[str, Any]] = []
        for item_id, item in pending:
            prov = build_provenance(item, status="summarized")
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "item_id": item_id,
                    "batch_id": None,
                    "source_platform": prov["source_platform"],
                    "source_url": prov["source_url"],
                    "raw_description": prov["raw_description"],
                    "collected_at": _approx_collected_at(item),
                    # 未知就是未知：不假装知道自己什么时候生成的摘要
                    "summary_generated_at": None,
                    "card_payload": item,
                    "status": prov["status"],
                    "backfilled": True,
                }
            )
        session.execute(
            pg_insert(CollectionRecord)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["item_id"])
        )
        session.commit()
        stats["backfilled"] = len(rows)
        return stats


# ==================== 编排 ====================

@dataclass
class CollectResult:
    """一次采集的结果摘要（路由与调度器共用）"""

    status: str  # ok | empty | error
    kind: str
    batch_id: Optional[str] = None
    fetched_count: int = 0
    new_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    message: Optional[str] = None


def _summary_applied(item: Dict[str, Any]) -> bool:
    """摘要是否成功落到卡片上（_apply_summary 必定写入 category）"""
    return "category" in item


async def collect_items(
    *,
    kind: str,
    fetch: Callable[[], Awaitable[List[Dict[str, Any]]]],
    store: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    trigger: str = BATCH_TRIGGER_MANUAL,
    operator_user_id: UserId = None,
    params: Optional[Dict[str, Any]] = None,
    force: bool = False,
    summarize: bool = True,
    persist: bool = True,
) -> CollectResult:
    """抓取 -> 去重 -> 摘要 -> 入库 -> 记录溯源 的统一编排

    Args:
        kind: repo | article
        fetch: 采集函数，返回原始卡片列表
        store: 写入知识库的回调（通常是 kb.upsert）；为 None 时只做溯源记录
        force: True 时忽略去重，全量重摘要并覆盖
        summarize: False 时跳过 LLM 摘要（卡片按原样归档）
        persist: False 时既不落溯源记录也不写 Redis 标记。知识库不可用的降级
            路径必须传 False —— 卡片其实没进知识库，若标记为「已采集」，
            等知识库恢复后这批卡片会被永久跳过
    """
    from src.modules.discovery.github_fetcher import summarize_items

    batch_id = start_batch(
        kind,
        trigger=trigger,
        operator_user_id=operator_user_id,
        params=params,
    )

    try:
        items = await fetch()
    except Exception as e:  # noqa: BLE001 - 采集异常要落批次再往上抛
        logger.error(f"❌ 采集失败({kind}): {e}")
        finish_batch(batch_id, status="failed", error=str(e)[:1000])
        raise

    if not items:
        finish_batch(batch_id, status="succeeded")
        return CollectResult(
            status="empty",
            kind=kind,
            batch_id=_sid(batch_id),
            message="未返回数据，可能触发限流或网络不可达",
        )

    fetched_count = len(items)
    ids = [str(i.get("id")) for i in items]
    if force:
        new_items, skipped_count = list(items), 0
    else:
        already = get_collected_ids(ids)
        new_items = [i for i in items if str(i.get("id")) not in already]
        skipped_count = fetched_count - len(new_items)

    if summarize and new_items:
        new_items = await summarize_items(new_items, kind=kind)

    failed_count = 0
    for item in new_items:
        item_id = str(item.get("id") or "")
        # 不要求摘要时，卡片按原样就是终态
        finalized = True if not summarize else _summary_applied(item)

        if store is not None:
            try:
                await store(item)
            except Exception as e:  # noqa: BLE001 - 单条入库失败不影响整批
                failed_count += 1
                if persist:
                    record_item(batch_id, item, status="failed")
                logger.warning(f"[WARN] 卡片入库失败: {item_id} - {e}")
                # 不写 Redis 标记：下次还能重试，避免「永久丢失」被缓存掩盖
                continue

        if finalized:
            if persist:
                record_item(batch_id, item, status="summarized")
                _redis_mark([item_id])
        else:
            failed_count += 1
            if persist:
                record_item(batch_id, item, status="failed")

    if failed_count == 0:
        batch_status = "succeeded"
    elif failed_count < len(new_items):
        batch_status = "partial"
    else:
        batch_status = "failed"

    finish_batch(
        batch_id,
        status=batch_status,
        fetched_count=fetched_count,
        new_count=len(new_items),
        skipped_count=skipped_count,
        failed_count=failed_count,
    )
    logger.info(
        f"✅ 采集完成({kind})：新增 {len(new_items)} 条，跳过 {skipped_count} 条，"
        f"失败 {failed_count} 条"
    )
    return CollectResult(
        status="ok",
        kind=kind,
        batch_id=_sid(batch_id),
        fetched_count=fetched_count,
        new_count=len(new_items),
        skipped_count=skipped_count,
        failed_count=failed_count,
    )


__all__ = [
    "BATCH_TRIGGER_MANUAL",
    "BATCH_TRIGGER_SCHEDULED",
    "PLATFORM_BY_SOURCE",
    "CollectResult",
    "backfill_records",
    "build_provenance",
    "collect_items",
    "finish_batch",
    "get_collected_ids",
    "get_provenance",
    "get_redis_client",
    "is_collected",
    "record_item",
    "start_batch",
]
