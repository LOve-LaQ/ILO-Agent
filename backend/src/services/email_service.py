# Email Service - 邮件发信设施
"""阶段 5 账号生命周期底座：找回密码、注销确认等邮件的统一出口。

**为什么做成「抽象 + 适配器」**：生产要真的投递（SMTP），开发环境却没有 SMTP
账号。若把「发信」写死成 SMTP 调用，本地根本跑不通找回密码这条链路，验收也就
无从谈起。于是拆成 `console`（写库 + 打日志）与 `smtp` 两种实现，业务代码只认
`send_email`，切换只改一个环境变量。

**为什么每封信都落 email_outbox**：
- console 模式下落库 = 开发/测试能从数据库直接取到重置链接，不必配真邮箱；
- smtp 模式下落库 = 留痕（发出时间、失败原因），用户说「没收到」时有据可查。

**为什么不强制 SMTP**：`EMAIL_PROVIDER=console` 在生产环境会被启动自检直接拦下
（见 services/captcha_service.assert_security_config），开发环境才允许降级。
"""

import asyncio
import threading
from typing import Optional

from loguru import logger

from src.core.config import settings
from src.core.db import get_session_factory, is_database_configured
from src.models.notification import EmailOutbox


class EmailNotifier:
    """发信适配器基类"""

    name = "base"

    def send(
        self,
        to_email: str,
        subject: str,
        body_text: str,
        *,
        body_html: Optional[str] = None,
        purpose: str = "generic",
    ) -> None:
        raise NotImplementedError


def _run_async(coro):
    """在同步上下文里执行一个协程。

    路由是同步 `def`（在线程池里跑），此时 `asyncio.run` 可用；但若将来在事件循环
    内部调用，`asyncio.run` 会抛 "already running"。这里做一层兼容：检测到运行中的
    事件循环就丢到独立线程执行，避免「发信把请求带崩」。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict = {}

    def _worker():
        box["value"] = asyncio.run(coro)

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    return box.get("value")


def record_outbox(
    to_email: str,
    subject: str,
    body_text: str,
    *,
    body_html: Optional[str],
    purpose: str,
    status: str,
    error: Optional[str] = None,
) -> None:
    """把一封邮件写进发件箱（best-effort，永不抛出 —— 发信失败不该让请求 500）"""
    if not is_database_configured():
        return
    from src.models.base import utcnow

    try:
        with get_session_factory()() as session:
            session.add(
                EmailOutbox(
                    to_email=to_email,
                    subject=subject,
                    body_text=body_text,
                    body_html=body_html,
                    purpose=purpose,
                    status=status,
                    error=error,
                    sent_at=utcnow() if status == "sent" else None,
                )
            )
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 邮件落库失败（不影响主流程）: {e}")


class ConsoleNotifier(EmailNotifier):
    """开发环境：不投递，只落库 + 打日志，便于直接从 DB/日志取链接"""

    name = "console"

    def send(
        self,
        to_email: str,
        subject: str,
        body_text: str,
        *,
        body_html: Optional[str] = None,
        purpose: str = "generic",
    ) -> None:
        record_outbox(
            to_email,
            subject,
            body_text,
            body_html=body_html,
            purpose=purpose,
            status="sent",
        )
        # 正文里含一次性链接，属联调需要；生产用 smtp，不会走到这里
        logger.info(f"[MAIL:console] -> {to_email} | {subject}\n{body_text}")


class SmtpNotifier(EmailNotifier):
    """生产环境：经 SMTP 真实投递（aiosmtplib）"""

    name = "smtp"

    def send(
        self,
        to_email: str,
        subject: str,
        body_text: str,
        *,
        body_html: Optional[str] = None,
        purpose: str = "generic",
    ) -> None:
        try:
            _run_async(self._send_async(to_email, subject, body_text, body_html))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WARN] 邮件发送失败: {e}")
            record_outbox(
                to_email,
                subject,
                body_text,
                body_html=body_html,
                purpose=purpose,
                status="failed",
                error=str(e),
            )
            return

        record_outbox(
            to_email,
            subject,
            body_text,
            body_html=body_html,
            purpose=purpose,
            status="sent",
        )

    async def _send_async(
        self, to_email: str, subject: str, body_text: str, body_html: Optional[str]
    ):
        # 延迟导入：未安装 aiosmtplib 时，只要不用 smtp 就不该影响应用启动
        from email.message import EmailMessage

        import aiosmtplib

        message = EmailMessage()
        message["From"] = settings.smtp_from or settings.smtp_user
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(body_text)
        if body_html:
            message.add_alternative(body_html, subtype="html")

        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user or None,
            password=settings.smtp_password or None,
            use_tls=settings.smtp_use_tls,
            timeout=10,
        )


def get_email_notifier() -> EmailNotifier:
    """按配置产出适配器（每调用一次：provider 可从环境变量热改）"""
    provider = (settings.email_provider or "").strip().lower()
    if provider == "smtp":
        return SmtpNotifier()
    return ConsoleNotifier()


def send_email(
    to_email: str,
    subject: str,
    body_text: str,
    *,
    body_html: Optional[str] = None,
    purpose: str = "generic",
) -> None:
    """统一发信入口。发信失败**不向上抛** —— 找回密码这类接口必须对「邮箱是否存在」
    返回完全一致的响应，发信异常若冒泡成 500 就泄露了这个差别。"""
    try:
        get_email_notifier().send(
            to_email,
            subject,
            body_text,
            body_html=body_html,
            purpose=purpose,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 发信失败（已吞掉，不影响接口响应）: {e}")


__all__ = [
    "ConsoleNotifier",
    "EmailNotifier",
    "SmtpNotifier",
    "get_email_notifier",
    "record_outbox",
    "send_email",
]
