# Retention Cleanup - 数据保留期清理与账号注销执行
"""阶段 5 数据生命周期：保留期清理 + 冷静期到期后的账号匿名化。

两类工作：
1. **账号注销落地**：冷静期（`ACCOUNT_DELETION_GRACE_DAYS`）到期后执行。
   采用**匿名化而非硬删**：把 email/username/password_hash 换成不可用的占位值、
   删除其个人数据行，但保留 `users` 行本身 —— 好处是外键完整性不破、邮箱被释放
   可重新注册；代价是留下一个不含个人信息的空壳行。硬删会把所有引用它的记录
   （含审计流水）一起带走或置空，反而更乱。
2. **保留期清理**：行为流水、会话、邮件、过期重置令牌按配置的保留天数删除。
   「收集了却永久留存」本身就是隐私问题，所以这里给每类数据都设了上限。

用法：
    python backend/retention_cleanup.py            # 实际执行
    python backend/retention_cleanup.py --dry-run  # 只统计不删除

可注册进 APScheduler 日调度（见 backend/news_scheduler.py 的调用方式）。
"""

import argparse
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict

from loguru import logger
from sqlalchemy import delete, func, or_, select

from src.core.config import settings
from src.core.db import get_session_factory, is_database_configured
from src.models.engagement import UserActivity, UserBookmark
from src.models.learning import ChatMessage, LearningSession
from src.models.notification import EmailOutbox
from src.models.password_reset import PasswordResetToken
from src.models.session import UserSession
from src.models.user import User

# 匿名化后密码字段换成一个绝不可能匹配的占位值（bcrypt 校验对它恒为 False）
_UNUSABLE_PASSWORD = "!deleted-account-no-login"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cutoff(days: int) -> datetime:
    return _now() - timedelta(days=max(0, days))


def anonymize_expired_deletions(db, *, dry_run: bool) -> int:
    """执行冷静期已过的注销：删除个人数据 + 匿名化 users 行。返回处理账号数。"""
    cutoff = _cutoff(settings.account_deletion_grace_days)
    users = list(
        db.execute(
            select(User).where(
                User.deletion_requested_at.is_not(None),
                User.deleted_at.is_(None),
                User.deletion_requested_at <= cutoff,
            )
        )
        .scalars()
        .all()
    )
    if not users:
        return 0

    processed = 0
    for user in users:
        session_ids = list(
            db.execute(
                select(LearningSession.session_id).where(
                    LearningSession.user_id == user.id
                )
            )
            .scalars()
            .all()
        )

        if not dry_run:
            # chat_messages 的 session_id 没有外键，必须显式按 session 清掉
            if session_ids:
                db.execute(
                    delete(ChatMessage).where(ChatMessage.session_id.in_(session_ids))
                )
            db.execute(delete(LearningSession).where(LearningSession.user_id == user.id))
            db.execute(delete(UserBookmark).where(UserBookmark.user_id == user.id))
            db.execute(delete(UserActivity).where(UserActivity.user_id == user.id))
            db.execute(delete(UserSession).where(UserSession.user_id == user.id))
            db.execute(
                delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
            )

            token = uuid.uuid4().hex
            user.email = f"deleted-{token}@deleted.invalid"
            user.username = f"deleted_{token[:8]}"
            user.password_hash = _UNUSABLE_PASSWORD
            user.is_active = False
            user.deleted_at = _now()

        processed += 1

    if not dry_run:
        db.commit()
    logger.info(
        f"[cleanup] 账号注销落地（{'dry-run' if dry_run else 'executed'}）: {processed} 个"
    )
    return processed


def purge_activities(db, *, dry_run: bool) -> int:
    cutoff = _cutoff(settings.activity_retention_days)
    stmt = delete(UserActivity).where(UserActivity.created_at < cutoff)
    if dry_run:
        return int(
            db.execute(
                select(func.count(UserActivity.id)).where(UserActivity.created_at < cutoff)
            ).scalar()
            or 0
        )
    result = db.execute(stmt)
    db.commit()
    return int(result.rowcount or 0)


def purge_sessions(db, *, dry_run: bool) -> int:
    """删除「已撤销」或「已过期」超过保留期的会话行"""
    cutoff = _cutoff(settings.session_retention_days)
    condition = or_(UserSession.revoked_at < cutoff, UserSession.expires_at < cutoff)
    if dry_run:
        return int(
            db.execute(select(func.count(UserSession.id)).where(condition)).scalar() or 0
        )
    result = db.execute(delete(UserSession).where(condition))
    db.commit()
    return int(result.rowcount or 0)


def purge_email_outbox(db, *, dry_run: bool) -> int:
    cutoff = _cutoff(settings.email_outbox_retention_days)
    condition = EmailOutbox.created_at < cutoff
    if dry_run:
        return int(
            db.execute(select(func.count(EmailOutbox.id)).where(condition)).scalar() or 0
        )
    result = db.execute(delete(EmailOutbox).where(condition))
    db.commit()
    return int(result.rowcount or 0)


def purge_password_reset_tokens(db, *, dry_run: bool) -> int:
    cutoff = _cutoff(settings.password_reset_retention_days)
    condition = PasswordResetToken.expires_at < cutoff
    if dry_run:
        return int(
            db.execute(
                select(func.count(PasswordResetToken.id)).where(condition)
            ).scalar()
            or 0
        )
    result = db.execute(delete(PasswordResetToken).where(condition))
    db.commit()
    return int(result.rowcount or 0)


def run_cleanup(*, dry_run: bool = False) -> Dict[str, int]:
    """执行全部清理任务，返回各项处理数量（供调度任务记录/断言）"""
    if not is_database_configured():
        logger.warning("[cleanup] 未配置数据库，跳过清理")
        return {}

    summary: Dict[str, int] = {}
    with get_session_factory()() as db:
        summary["deleted_accounts"] = anonymize_expired_deletions(db, dry_run=dry_run)
        summary["activities"] = purge_activities(db, dry_run=dry_run)
        summary["sessions"] = purge_sessions(db, dry_run=dry_run)
        summary["emails"] = purge_email_outbox(db, dry_run=dry_run)
        summary["reset_tokens"] = purge_password_reset_tokens(db, dry_run=dry_run)

    logger.info(f"[cleanup] 完成: {summary}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="ILO 数据保留期清理与账号注销执行")
    parser.add_argument(
        "--dry-run", action="store_true", help="只统计将要处理的行数，不实际修改"
    )
    args = parser.parse_args()
    summary = run_cleanup(dry_run=args.dry_run)
    for key, value in summary.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
