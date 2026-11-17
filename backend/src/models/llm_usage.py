# Models - LLM 调用账单
"""`llm_usage` 表：一次 LLM 调用一行，用于成本可观测与预测。

## 为什么不是「按天聚合后落库」
聚合表能被一次 `UPDATE` 改错，而明细行永远不会「算错」——单价变了、聚合口径变了，
都能拿原始明细重算。明细一天几十~几千行，量级完全撑得住；先存明细是这类账本的
标准做法（可审计），需要性能时再加物化视图。

## 为什么金额要落库
`cost_cny` 在**写入时**按当时的单价算好。若留在查询时按当前单价乘历史 token，
上游一调价，过去几个月的账会被集体改写，成本曲线会出现一个不存在的台阶。
落库后「历史不可变」，只有新调用用新价。

## 为什么记 `tokens_estimated`
不是每次调用都能拿到上游的 `usage`（部分网关不返回）。此时退回字符估算，
账单上必须能区分「真实计量」与「估算」—— 否则做预算决策的人无从判断误差来源。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class LlmUsage(Base):
    """一次 LLM 调用的用量与花费"""

    __tablename__ = "llm_usage"
    __table_args__ = (
        # 成本面板的主查询是「最近 N 天、按用途/按天聚合」，
        # created_at 单列索引足够；再加 (purpose, created_at) 的收益不如明细表增长快。
        Index("ix_llm_usage_created_at", "created_at"),
        Index("ix_llm_usage_purpose", "purpose"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 业务用途（explain / chat / digest / summary /embedding…），成本归因的第一维度
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    # 模型与实际 Provider：换模型后能看出「省了多少 / 贵在哪」
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    # 谁触发的（后台批量任务为空）。SET NULL：删用户不该带走这段成本史
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Numeric 而不是 Float：钱不能用二进制浮点累加，尾差会在月度汇总里显形
    cost_cny: Mapped[float] = mapped_column(
        Numeric(12, 6), nullable=False, default=0
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 失败也记账：失败率是「成本预测」的必要输入（重试会重复计费），
    # 但它不产生金额，查询汇总时按 success 过滤成本。
    success: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    tokens_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # 与日志里的 request_id 对齐，出账异常时能回溯到具体请求
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<LlmUsage {self.purpose} {self.model} "
            f"{self.total_tokens}tok ¥{self.cost_cny}>"
        )
