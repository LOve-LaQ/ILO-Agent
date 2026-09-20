# Tests - 邮箱确认邮件（非阻塞 / 幂等 / typ 隔离）
"""阶段 5 补充验收：注册后发出的那封邮箱确认邮件。

需要 DATABASE_URL（本机 PostgreSQL + backend/.env），缺则整体跳过。

开发环境 EMAIL_PROVIDER=console：邮件只落 email_outbox、不真正投递，因此测试
可以直接从库里取出确认链接里的令牌，无需真实邮箱。
"""

import re
import uuid

import pytest
from sqlalchemy import delete, select

from src.core.config import settings
from src.core.db import get_session_factory, is_database_configured
from src.models.engagement import UserActivity
from src.models.notification import EmailOutbox
from src.models.user import User

pytestmark = pytest.mark.skipif(
    not is_database_configured(),
    reason="需要 DATABASE_URL（本机 PostgreSQL + backend/.env）",
)

EMAIL_PREFIX = "verify_"
PASSWORD = "Str0ng-Passw0rd"

# 确认链接里的令牌是 JWT（base64url 段用 . 连接），字符集比 token_urlsafe 多一个点
TOKEN_RE = re.compile(r"token=([A-Za-z0-9_.\-]+)")


def _payload() -> dict:
    token = uuid.uuid4().hex[:10]
    return {
        "email": f"{EMAIL_PREFIX}{token}@example.com",
        "username": f"verify_{token}",
        "password": PASSWORD,
    }


def _hdr(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _register(client) -> dict:
    resp = client.post("/api/v1/auth/register", json=_payload())
    assert resp.status_code == 201, resp.text
    return resp.json()


def _confirm_emails(email: str) -> list[EmailOutbox]:
    with get_session_factory()() as session:
        return list(
            session.execute(
                select(EmailOutbox)
                .where(
                    EmailOutbox.to_email == email,
                    EmailOutbox.purpose == "email_confirm",
                )
                .order_by(EmailOutbox.created_at.asc())
            )
            .scalars()
            .all()
        )


def _latest_confirm_token(email: str) -> str:
    rows = _confirm_emails(email)
    assert rows, "注册后必须落一封确认邮件到 email_outbox"
    match = TOKEN_RE.search(rows[-1].body_text)
    assert match, f"邮件正文里应当包含确认链接：{rows[-1].body_text!r}"
    return match.group(1)


def _purge() -> None:
    with get_session_factory()() as session:
        user_ids = (
            session.execute(select(User.id).where(User.email.like(f"{EMAIL_PREFIX}%")))
            .scalars()
            .all()
        )
        # email_outbox 没有指向用户的外键，按收件地址清理
        session.execute(
            delete(EmailOutbox).where(EmailOutbox.to_email.like(f"{EMAIL_PREFIX}%"))
        )
        if user_ids:
            session.execute(
                delete(UserActivity).where(UserActivity.user_id.in_(user_ids))
            )
            session.execute(delete(User).where(User.id.in_(user_ids)))
        session.commit()


@pytest.fixture(scope="module", autouse=True)
def _cleanup_test_users():
    _purge()
    yield
    _purge()


@pytest.fixture(autouse=True)
def _isolated_cookie_jar(anonymous_client):
    anonymous_client.cookies.clear()
    yield
    anonymous_client.cookies.clear()


# ==================== 发信 ====================


def test_register_queues_confirmation_email(anonymous_client):
    """注册即发确认邮件，且**不阻塞**注册本身（注册已经 201 成功了）。"""
    registered = _register(anonymous_client)
    email = registered["user"]["email"]

    rows = _confirm_emails(email)
    assert len(rows) == 1, "注册应当恰好发出一封确认邮件"
    assert rows[0].status == "sent"
    assert "/verify-email?token=" in rows[0].body_text


def test_register_reports_email_unverified(anonymous_client):
    """新注册的账号对外应报 email_verified=false（这只是提示，不影响使用）。"""
    registered = _register(anonymous_client)
    assert registered["user"]["email_verified"] is False


# ==================== 确认 ====================


def test_confirm_email_marks_verified(anonymous_client):
    registered = _register(anonymous_client)
    email = registered["user"]["email"]
    access = registered["access_token"]

    resp = anonymous_client.post(
        "/api/v1/auth/email/confirm", json={"token": _latest_confirm_token(email)}
    )
    assert resp.status_code == 200, resp.text

    me = anonymous_client.get("/api/v1/auth/me", headers=_hdr(access))
    assert me.status_code == 200, me.text
    assert me.json()["email_verified"] is True


def test_confirm_is_idempotent(anonymous_client):
    """用户常连点两三次，重复确认必须返回同样的成功话术，不能报错。"""
    registered = _register(anonymous_client)
    email = registered["user"]["email"]
    token = _latest_confirm_token(email)

    first = anonymous_client.post("/api/v1/auth/email/confirm", json={"token": token})
    second = anonymous_client.post("/api/v1/auth/email/confirm", json={"token": token})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json()


def test_confirm_rejects_forged_token(anonymous_client):
    resp = anonymous_client.post(
        "/api/v1/auth/email/confirm", json={"token": "not-a-real-token"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "EMAIL_CONFIRM_INVALID"


def test_confirm_rejects_access_token(anonymous_client):
    """安全关键：登录用的 access token 绝不能拿来当确认凭据（靠 `typ` 隔离）。"""
    registered = _register(anonymous_client)
    access = registered["access_token"]

    resp = anonymous_client.post("/api/v1/auth/email/confirm", json={"token": access})
    assert resp.status_code == 400
    assert resp.json()["code"] == "EMAIL_CONFIRM_INVALID"

    # 且不能因为这次尝试就把邮箱标记成已验证
    me = anonymous_client.get("/api/v1/auth/me", headers=_hdr(access))
    assert me.json()["email_verified"] is False


def test_expired_confirm_token_is_rejected(monkeypatch, anonymous_client):
    """令牌过期即拒绝（把有效期压到 0 小时来构造过期）。

    注意补丁必须在**注册之前**打：确认令牌是注册那一刻按当时的有效期签发的，
    注册后再改配置不会追溯影响已发出的那封信。
    """
    monkeypatch.setattr(settings, "email_verification_expire_hours", 0)
    registered = _register(anonymous_client)
    token = _latest_confirm_token(registered["user"]["email"])

    resp = anonymous_client.post("/api/v1/auth/email/confirm", json={"token": token})
    assert resp.status_code == 400
    assert resp.json()["code"] == "EMAIL_CONFIRM_INVALID"


# ==================== 重发 ====================


def test_resend_requires_login(anonymous_client):
    resp = anonymous_client.post("/api/v1/auth/email/resend")
    assert resp.status_code == 401


def test_resend_sends_another_email_then_stops_once_verified(anonymous_client):
    """重发是「信丢了」的唯一补救入口；确认之后则应停止重发。"""
    registered = _register(anonymous_client)
    email = registered["user"]["email"]
    access = registered["access_token"]

    resend = anonymous_client.post("/api/v1/auth/email/resend", headers=_hdr(access))
    assert resend.status_code == 200, resend.text
    assert len(_confirm_emails(email)) == 2, "重发应当真的再发一封"

    token = _latest_confirm_token(email)
    assert (
        anonymous_client.post(
            "/api/v1/auth/email/confirm", json={"token": token}
        ).status_code
        == 200
    )

    after = anonymous_client.post("/api/v1/auth/email/resend", headers=_hdr(access))
    assert after.status_code == 200, after.text
    assert len(_confirm_emails(email)) == 2, "已验证后不应再发信"
