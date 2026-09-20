# Tests - 找回密码（防枚举 / 一次性 / 过期 / 会话全撤）
"""阶段 5 找回密码验收：走真实发信（console → email_outbox）+ 真实 PostgreSQL。

需要 DATABASE_URL（本机 PostgreSQL + backend/.env），缺则整体跳过。

开发环境 EMAIL_PROVIDER=console：邮件只落 email_outbox、不真正投递，因此
测试可以直接从库里取出重置链接里的 token，无需真实邮箱。
"""

import json
import re
import uuid

import pytest
from sqlalchemy import delete, select

from src.core.config import settings
from src.core.db import get_session_factory, is_database_configured
from src.models.engagement import UserActivity
from src.models.learning import ChatMessage, LearningSession
from src.models.notification import EmailOutbox
from src.models.user import User

pytestmark = pytest.mark.skipif(
    not is_database_configured(),
    reason="需要 DATABASE_URL（本机 PostgreSQL + backend/.env）",
)

EMAIL_PREFIX = "reset_"
PASSWORD = "Str0ng-Passw0rd"
NEW_PASSWORD = "N3w-Str0ng-Pass"

# token_urlsafe 的字符集：A-Za-z0-9_-
TOKEN_RE = re.compile(r"token=([A-Za-z0-9_\-]+)")


def _unique() -> str:
    return uuid.uuid4().hex[:10]


def _payload() -> dict:
    token = _unique()
    return {
        "email": f"{EMAIL_PREFIX}{token}@example.com",
        "username": f"reset_{token}",
        "password": PASSWORD,
    }


def _hdr(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _register(client) -> dict:
    resp = client.post("/api/v1/auth/register", json=_payload())
    assert resp.status_code == 201, resp.text
    return resp.json()


def _forgot(client, email: str):
    return client.post("/api/v1/auth/password/forgot", json={"email": email})


def _latest_reset_token(email: str) -> str:
    with get_session_factory()() as session:
        row = (
            session.execute(
                select(EmailOutbox)
                .where(
                    EmailOutbox.to_email == email,
                    EmailOutbox.purpose == "password_reset",
                )
                .order_by(EmailOutbox.created_at.desc())
            )
            .scalars()
            .first()
        )
    assert row is not None, "找回密码必须落一封重置邮件到 email_outbox"
    match = TOKEN_RE.search(row.body_text)
    assert match, f"邮件正文里应当包含重置链接：{row.body_text!r}"
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
        if not user_ids:
            session.commit()
            return
        session_ids = (
            session.execute(
                select(LearningSession.session_id).where(
                    LearningSession.user_id.in_(user_ids)
                )
            )
            .scalars()
            .all()
        )
        if session_ids:
            session.execute(
                delete(ChatMessage).where(ChatMessage.session_id.in_(session_ids))
            )
        session.execute(delete(UserActivity).where(UserActivity.user_id.in_(user_ids)))
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


# ==================== 防枚举 ====================


def test_forgot_password_identical_response_for_known_and_unknown(anonymous_client):
    """邮箱存在与否，响应必须完全一致，且绝不回吐 token。"""
    registered = _register(anonymous_client)
    email = registered["user"]["email"]

    known = _forgot(anonymous_client, email)
    unknown = _forgot(anonymous_client, f"{EMAIL_PREFIX}ghost@example.com")

    assert known.status_code == 200, known.text
    assert unknown.status_code == 200, unknown.text
    assert known.json() == unknown.json()

    lowered = json.dumps(known.json()).lower()
    assert "token" not in lowered
    assert "password" not in lowered


def test_forgot_password_debounces_duplicate_emails(anonymous_client):
    """同一邮箱 60 秒内重复申请不重复发信（防邮件轰炸）。"""
    registered = _register(anonymous_client)
    email = registered["user"]["email"]

    _forgot(anonymous_client, email)
    _forgot(anonymous_client, email)

    with get_session_factory()() as session:
        rows = (
            session.execute(
                select(EmailOutbox).where(
                    EmailOutbox.to_email == email,
                    EmailOutbox.purpose == "password_reset",
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, "防轰炸窗口内不应重复发信"


# ==================== 完整重置流程 ====================


def test_reset_password_flow_changes_password(anonymous_client):
    registered = _register(anonymous_client)
    email = registered["user"]["email"]

    assert _forgot(anonymous_client, email).status_code == 200
    token = _latest_reset_token(email)

    reset = anonymous_client.post(
        "/api/v1/auth/password/reset",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert reset.status_code == 200, reset.text

    new_login = anonymous_client.post(
        "/api/v1/auth/login", json={"identifier": email, "password": NEW_PASSWORD}
    )
    assert new_login.status_code == 200, new_login.text

    old_login = anonymous_client.post(
        "/api/v1/auth/login", json={"identifier": email, "password": PASSWORD}
    )
    assert old_login.status_code == 401


def test_reset_token_is_single_use(anonymous_client):
    registered = _register(anonymous_client)
    email = registered["user"]["email"]

    _forgot(anonymous_client, email)
    token = _latest_reset_token(email)

    first = anonymous_client.post(
        "/api/v1/auth/password/reset",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert first.status_code == 200, first.text

    second = anonymous_client.post(
        "/api/v1/auth/password/reset",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert second.status_code == 400
    assert second.json()["code"] == "RESET_TOKEN_INVALID"


def test_expired_reset_token_is_rejected(monkeypatch, anonymous_client):
    """令牌过期即拒绝（把有效期压到 0 秒来构造过期）。"""
    registered = _register(anonymous_client)
    email = registered["user"]["email"]

    monkeypatch.setattr(settings, "password_reset_expire_minutes", 0)
    _forgot(anonymous_client, email)
    token = _latest_reset_token(email)

    resp = anonymous_client.post(
        "/api/v1/auth/password/reset",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "RESET_TOKEN_INVALID"


def test_reset_password_revokes_all_sessions(anonymous_client, fake_redis):
    """重置密码后旧凭证一律作废：两台设备的 access 都应立即失效。"""
    registered = _register(anonymous_client)
    email = registered["user"]["email"]
    access_a = registered["access_token"]

    login = anonymous_client.post(
        "/api/v1/auth/login", json={"identifier": email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    access_b = login.json()["access_token"]

    _forgot(anonymous_client, email)
    token = _latest_reset_token(email)

    reset = anonymous_client.post(
        "/api/v1/auth/password/reset",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert reset.status_code == 200, reset.text

    for access in (access_a, access_b):
        me = anonymous_client.get("/api/v1/auth/me", headers=_hdr(access))
        assert me.status_code == 401, f"会话未撤销: {access}"
        assert me.json()["code"] == "SESSION_REVOKED"
