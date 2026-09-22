# Tests - 账号注销（冷静期 / 撤销注销 / 匿名化落地）
"""阶段 5 账号生命周期验收：走真实注销链路（真实 PostgreSQL）。

需要 DATABASE_URL（本机 PostgreSQL + backend/.env），缺则整体跳过。

匿名化用例直接调用 `backend/retention_cleanup.anonymize_expired_deletions`
（与调度任务同一条代码路径），把冷静期压到 0 天来复现「到期执行」。

清理按**用户 id** 进行：匿名化会把 email 换成 deleted-*@deleted.invalid，
按邮箱前缀找不到这些空壳行，只能记住 id。
"""

import uuid

import pytest
from sqlalchemy import delete, select

import retention_cleanup
from src.core.config import settings
from src.core.db import get_session_factory, is_database_configured
from src.models.engagement import UserActivity, UserBookmark
from src.models.learning import ChatMessage, LearningSession
from src.models.notification import EmailOutbox
from src.models.session import UserSession
from src.models.user import User

pytestmark = pytest.mark.skipif(
    not is_database_configured(),
    reason="需要 DATABASE_URL（本机 PostgreSQL + backend/.env）",
)

EMAIL_PREFIX = "del_"
PASSWORD = "Str0ng-Passw0rd"
UNUSABLE_PASSWORD = "!deleted-account-no-login"

# 本模块创建的账号 id：匿名化后靠它定位空壳行
_CREATED_IDS: set = set()


def _unique() -> str:
    return uuid.uuid4().hex[:10]


def _payload(**overrides) -> dict:
    token = _unique()
    payload = {
        "email": f"{EMAIL_PREFIX}{token}@example.com",
        "username": f"del_{token}",
        "password": PASSWORD,
    }
    payload.update(overrides)
    return payload


def _hdr(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _register(client, **overrides) -> dict:
    resp = client.post("/api/v1/auth/register", json=_payload(**overrides))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    _CREATED_IDS.add(body["user"]["id"])
    return body


def _purge() -> None:
    """删除本模块造的用户及其衍生数据（含匿名化后的空壳行）。"""
    with get_session_factory()() as session:
        ids = set(_CREATED_IDS)
        # 自愈：上一轮意外中断留下的、带固定前缀的账号
        ids |= set(
            session.execute(
                select(User.id).where(User.email.like(f"{EMAIL_PREFIX}%"))
            )
            .scalars()
            .all()
        )
        # 自愈：被匿名化的空壳行（email/username 同时命中匿名化形态，特征明确）
        ids |= set(
            session.execute(
                select(User.id).where(
                    User.email.like("deleted-%@deleted.invalid"),
                    User.username.like("deleted_%"),
                )
            )
            .scalars()
            .all()
        )
        if not ids:
            return
        user_ids = [uuid.UUID(str(value)) for value in ids]
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


# ==================== 申请注销与冷静期 ====================


def test_delete_enters_grace_and_revokes_sessions(anonymous_client):
    registered = _register(anonymous_client)
    user_id = registered["user"]["id"]
    access = registered["access_token"]

    # 第二台设备，用于验证「全部会话被撤销」
    anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": registered["user"]["email"], "password": PASSWORD},
    )

    resp = anonymous_client.post(
        "/api/v1/auth/account/delete",
        json={
            "password": PASSWORD,
            "confirm_username": registered["user"]["username"],
        },
        headers=_hdr(access),
    )
    assert resp.status_code == 200, resp.text

    with get_session_factory()() as session:
        user = session.get(User, uuid.UUID(user_id))
        assert user.is_active is False
        assert user.deletion_requested_at is not None
        assert user.deleted_at is None, "冷静期内绝不能先行落地删除"
        active = (
            session.execute(
                select(UserSession.id).where(
                    UserSession.user_id == uuid.UUID(user_id),
                    UserSession.revoked_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        assert active == [], "申请注销必须撤销全部会话"
        mail = (
            session.execute(
                select(EmailOutbox).where(
                    EmailOutbox.to_email == registered["user"]["email"],
                    EmailOutbox.purpose == "account_deletion",
                )
            )
            .scalars()
            .first()
        )
        assert mail is not None, "应发出注销确认邮件"

    # 冷静期内登录：明确提示已申请注销
    blocked = anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": registered["user"]["email"], "password": PASSWORD},
    )
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["code"] == "ACCOUNT_PENDING_DELETION"


def test_delete_requires_password_and_matching_username(anonymous_client):
    registered = _register(anonymous_client)
    user_id = registered["user"]["id"]
    access = registered["access_token"]

    wrong_pw = anonymous_client.post(
        "/api/v1/auth/account/delete",
        json={"password": "Wr0ng-Passw0rd", "confirm_username": registered["user"]["username"]},
        headers=_hdr(access),
    )
    assert wrong_pw.status_code == 400
    assert wrong_pw.json()["code"] == "INVALID_CREDENTIALS"

    wrong_name = anonymous_client.post(
        "/api/v1/auth/account/delete",
        json={"password": PASSWORD, "confirm_username": "someone-else"},
        headers=_hdr(access),
    )
    assert wrong_name.status_code == 400
    assert wrong_name.json()["code"] == "CONFIRM_MISMATCH"

    # 两次失败都不应产生任何副作用
    with get_session_factory()() as session:
        user = session.get(User, uuid.UUID(user_id))
        assert user.is_active is True
        assert user.deletion_requested_at is None


def test_delete_cancel_restores_account(anonymous_client):
    registered = _register(anonymous_client)
    user_id = registered["user"]["id"]
    email = registered["user"]["email"]

    anonymous_client.post(
        "/api/v1/auth/account/delete",
        json={"password": PASSWORD, "confirm_username": registered["user"]["username"]},
        headers=_hdr(registered["access_token"]),
    )

    cancel = anonymous_client.post(
        "/api/v1/auth/account/delete/cancel",
        json={"identifier": email, "password": PASSWORD},
    )
    assert cancel.status_code == 200, cancel.text

    with get_session_factory()() as session:
        user = session.get(User, uuid.UUID(user_id))
        assert user.is_active is True
        assert user.deletion_requested_at is None

    login = anonymous_client.post(
        "/api/v1/auth/login", json={"identifier": email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text


def test_cancel_without_pending_is_rejected(anonymous_client):
    """没有待处理注销申请时，撤销必须被拒 —— 且**不能**与「密码错误」可区分。

    这个端点匿名可达、以密码为唯一凭证。如果「账号是否存在 / 有没有待注销申请」
    能被单独探测出来，就等于送出一个匿名可用的账号状态预言机。所以两种情况必须
    返回同一个 code / 状态码 / 话术。
    """
    registered = _register(anonymous_client)
    email = registered["user"]["email"]

    no_pending = anonymous_client.post(
        "/api/v1/auth/account/delete/cancel",
        json={"identifier": email, "password": PASSWORD},
    )
    wrong_password = anonymous_client.post(
        "/api/v1/auth/account/delete/cancel",
        json={"identifier": email, "password": "Wr0ng-Passw0rd"},
    )

    assert no_pending.status_code == 401
    assert no_pending.json()["code"] == "INVALID_CREDENTIALS"
    # 关键：两种失败响应完全一致，调用方无法据此判断账号状态或密码是否正确
    assert no_pending.status_code == wrong_password.status_code
    assert no_pending.json()["code"] == wrong_password.json()["code"]
    assert no_pending.json()["message"] == wrong_password.json()["message"]


# ==================== 冷静期到期 → 匿名化落地 ====================


def test_anonymization_purges_data_and_frees_email(monkeypatch, anonymous_client):
    registered = _register(anonymous_client)
    user_id = registered["user"]["id"]
    email = registered["user"]["email"]
    access = registered["access_token"]

    # 造一点个人数据：收藏 + 学习会话
    bookmark = anonymous_client.post(
        "/api/v1/me/bookmarks/atdel-card", headers=_hdr(access)
    )
    assert bookmark.status_code == 200, bookmark.text
    created = anonymous_client.post(
        "/api/v1/learning/session",
        json={"news_item_id": "atdel-news", "title": "注销测试主题"},
        headers=_hdr(access),
    )
    assert created.status_code == 200, created.text

    # 申请注销（进入冷静期）
    requested = anonymous_client.post(
        "/api/v1/auth/account/delete",
        json={"password": PASSWORD, "confirm_username": registered["user"]["username"]},
        headers=_hdr(access),
    )
    assert requested.status_code == 200, requested.text

    # 冷静期到期 → 匿名化（把 grace 压到 0 天）
    monkeypatch.setattr(settings, "account_deletion_grace_days", 0)
    with get_session_factory()() as session:
        processed = retention_cleanup.anonymize_expired_deletions(session, dry_run=False)
    assert processed >= 1

    with get_session_factory()() as session:
        user = session.get(User, uuid.UUID(user_id))
        assert user is not None, "匿名化而非硬删：users 行必须保留以维持外键完整性"
        assert user.deleted_at is not None
        assert user.is_active is False
        assert user.email.endswith("@deleted.invalid")
        assert user.username.startswith("deleted_")
        assert user.password_hash == UNUSABLE_PASSWORD

        # 个人数据清干净（bookmarks / sessions / activities 都不再属于该用户）
        assert (
            session.execute(
                select(UserBookmark).where(UserBookmark.user_id == uuid.UUID(user_id))
            )
            .scalars()
            .first()
            is None
        )
        assert (
            session.execute(
                select(LearningSession).where(
                    LearningSession.user_id == uuid.UUID(user_id)
                )
            )
            .scalars()
            .first()
            is None
        )
        assert (
            session.execute(
                select(UserActivity).where(UserActivity.user_id == uuid.UUID(user_id))
            )
            .scalars()
            .first()
            is None
        )

    # 邮箱被释放，可重新注册
    reborn = _register(anonymous_client, email=email)
    assert reborn["user"]["email"] == email
    assert reborn["user"]["id"] != user_id
