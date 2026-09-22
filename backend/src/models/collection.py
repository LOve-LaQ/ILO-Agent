# Models - 内容溯源
"""采集批次与采集记录（阶段 3 内容溯源）。

这两张表回答「这张卡片是哪次抓取、从哪个链接、什么时候来的」：

- `collection_batches`：一次抓取动作（手动点「抓取最新」或定时任务）→ 一条批次
- `collection_records`：一张卡片 → 一条记录，`item_id` 唯一

`collection_records.item_id` 的唯一约束把此前只存在 Redis 的 `crawled:item_ids`
固化为**可追溯的真相源**；Redis 降级为加速缓存并保持双写。
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from src.models.base import Base

# 批次触发方式 / 状态（受控词表，避免各调用点写错字符串）
BATCH_TRIGGERS = ("manual", "scheduled")
BATCH_STATUSES = ("running", "succeeded", "partial", "failed")
RECORD_STATUSES = ("summarized", "deduped", "failed")


class CollectionBatch(Base):
    """一次采集动作（含参数、结果计数与耗时）"""

    __tablename__ = "collection_batches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # repo | article
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # manual | scheduled
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    # 手动触发时记录操作人；定时任务为 NULL（ondelete=SET NULL 保留批次历史）
    operator_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 本次抓取参数（limit / per_platform / time_range / force），用于复现
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    fetched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<CollectionBatch {self.kind}/{self.trigger} {self.status}>"


class CollectionRecord(Base):
    """一张卡片的采集溯源记录（item_id 唯一 = 去重的真相源）"""

    __tablename__ = "collection_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 与卡片 id 一致（gh-123 / hn-abc / dev-1 / news-001 ...）
    item_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("collection_batches.id", ondelete="SET NULL"),
        nullable=True,
    )

    # 来源三件套：平台、原始链接、原始描述（摘要生成前的原文）
    source_platform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 原文快照（仓库 README）：用户在「先读原文」页看到的就是它。
    # 首次写入即定稿 —— force 重摘要不会改写已有快照，保证「这张卡片采集时刻的原文」
    # 永远可回溯；缺失时可在首次打开时按需补抓一次（见 collection_service）。
    raw_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 快照元信息：{path, sha, size, source, fetched_at}，用于回答「读的是哪个版本」
    content_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # 中文导读：大量仓库 README 只有英文，中文用户点开「先读原文」等于读不懂。
    # 这里存一份**按需生成**的中文导读（不是逐字译文，而是「这仓库是什么、解决什么、
    # 适合谁」的中文概览）。与 raw_content 一样是「生成一次、长期复用」的产物，
    # 因此同样落在真相源表上，而不是塞进缓存。
    # 为空 = 还没人生成过（而不是「生成了但没有」）。
    content_digest_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 导读元信息：{source_fingerprint, source_chars, generated_at}。
    # `source_fingerprint` 绑定的是**生成时那份原文**（sha 优先，raw CDN 路径退回
    # 内容摘要），原文一改指纹就变，导读自动失效重生成 —— 否则会出现「译文对不上
    # 原文版本」这种最难排查的错配。
    content_digest_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    summary_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 入库当时的完整卡片快照：卡片后来被覆盖/删除也能回溯当初长什么样
    card_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # summarized | deduped | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="summarized")
    # 历史数据回填标记：老卡片没有真实批次信息，用近似值并显式标注
    backfilled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=expression.false()
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<CollectionRecord {self.item_id} {self.status}>"
