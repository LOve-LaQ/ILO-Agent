# Tests - 会话安全（refresh 轮换 / 并发宽限 / reuse 检测 / 撤销即时生效）
"""阶段 5 会话可撤销验收：走真实注册 / 登录 / 刷新链路（真实 PostgreSQL）。

需要 DATABASE_URL（本机 PostgreSQL + backend/.env），缺则整体跳过。

涉及「sid 黑名单即时生效」（撤销后 access 立刻 401）的用例注入 fakeredis，
让黑名单落在内存；其余用例只依赖数据库里的会话行。
"""

import time
import uuid

import pytest
from sqlalchemy import delete, select

from src.core.config import settings
from src.core.db import get_session_factory, is_database_configured
from src.models.engagement import UserActivity
from src.models.learning import ChatMessage, LearningSession
from src.models.session import UserSession
from src.models.user import User

pytestmark = pytest.mark.skipif(
    not is_database_configured(),
    reason="需要 DATABASE_URL（本机 PostgreSQL + backend/.env）",
)

EMAIL_PREFIX = "sess_"
PASSWORD = "Str0ng-Passw0rd"
NEW_PASSWORD = "N3w-Str0ng-Pass"


def _unique() -> str:
    return uuid.uuid4().hex[:10]


def _payload() -> dict:
    token = _unique()
    return {
        "email": f"{EMAIL_PREFIX}{token}@example.com",
        "username": f"sess_{token}",
        "password": PASSWORD,
    }


def _hdr(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _register(client) -> dict:
    resp = client.post("/api/v1/auth/register", json=_payload())
    assert resp.status_code == 201, resp.text
    return resp.json()


def _refresh_with(client, token: str):
    """显式带上某一枚 refresh token 发起刷新。

    先清空 Cookie jar，再用 Cookie 头传指定的 token —— 否则 jar 里「最新」的
    Cookie 会盖过我们想重放的旧 token。
    """
    client.cookies.clear()
    return client.post(
        "/api/v1/auth/refresh",
        headers={"Cookie": f"{settings.refresh_cookie_name}={token}"},
    )


def _sessions_of(user_id: str) -> list[UserSession]:
    with get_session_factory()() as session:
        return list(
            session.execute(
                select(UserSession).where(UserSession.user_id == uuid.UUID(user_id))
            )
            .scalars()
            .all()
        )


def _active_session_count(user_id: str) -> int:
    with get_session_factory()() as session:
        return len(
            session.execute(
                select(UserSession.id).where(
                    UserSession.user_id == uuid.UUID(user_id),
                    UserSession.revoked_at.is_(None),
                )
            )
            .scalars()
            .all()
        )


def _purge() -> None:
    with get_session_factory()() as session:
        user_ids = (
            session.execute(select(User.id).where(User.email.like(f"{EMAIL_PREFIX}%")))
            .scalars()
            .all()
        )
        if not user_ids:
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


# ==================== refresh 轮换 ====================


def test_refresh_rotates_and_revokes_old_session(anonymous_client):
    """刷新页面换发新 refresh，并作废旧会话、串上替换链。"""
    registered = _register(anonymous_client)
    user_id = registered["user"]["id"]
    old_cookie = anonymous_client.cookies.get(settings.refresh_cookie_name)
    assert old_cookie

    refreshed = anonymous_client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200, refreshed.text
    new_cookie = anonymous_client.cookies.get(settings.refresh_cookie_name)
    assert new_cookie and new_cookie != old_cookie, "轮换必须换发新 refresh"
    assert settings.refresh_cookie_name in refreshed.headers.get("set-cookie", "")

    rows = _sessions_of(user_id)
    assert len(rows) == 2, "应当保留旧行（用于 reuse 判定）+ 新行"
    revoked = [row for row in rows if row.revoked_at is not None]
    active = [row for row in rows if row.revoked_at is None]
    assert len(revoked) == 1 and len(active) == 1
    assert revoked[0].replaced_by_jti == active[0].refresh_jti


def test_concurrent_refresh_within_grace_only_reissues_access(anonymous_client):
    """多标签页并发：旧 token 在宽限窗内再次出现时，只补发 access，不换发 refresh。"""
    registered = _register(anonymous_client)
    first_token = anonymous_client.cookies.get(settings.refresh_cookie_name)

    first = anonymous_client.post("/api/v1/auth/refresh")
    assert first.status_code == 200, first.text

    replay = _refresh_with(anonymous_client, first_token)
    assert replay.status_code == 200, replay.text
    # 并发宽限路径不再下发新 refresh：没有 Set-Cookie
    assert "set-cookie" not in replay.headers

    me = anonymous_client.get(
        "/api/v1/auth/me", headers=_hdr(replay.json()["access_token"])
    )
    assert me.status_code == 200
    assert me.json()["id"] == registered["user"]["id"]


# ==================== reuse 检测 ====================


def test_reuse_beyond_grace_revokes_all_sessions(
    monkeypatch, anonymous_client, fake_redis
):
    """已作废 refresh 在宽限窗外再次出现 → 判为盗用，撤销该用户全部会话。"""
    monkeypatch.setattr(settings, "session_rotate_grace_seconds", 0)

    registered = _register(anonymous_client)
    user_id = registered["user"]["id"]
    first_token = anonymous_client.cookies.get(settings.refresh_cookie_name)

    # 第二台设备（再登录一次）
    login = anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": registered["user"]["email"], "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    assert _active_session_count(user_id) == 2

    # 先用旧 token 正常轮换一次
    rotated = _refresh_with(anonymous_client, first_token)
    assert rotated.status_code == 200, rotated.text

    time.sleep(0.05)  # 确保超出宽限窗（grace=0）
    replay = _refresh_with(anonymous_client, first_token)
    assert replay.status_code == 401, replay.text
    assert replay.json()["code"] == "TOKEN_REUSE"

    assert _active_session_count(user_id) == 0, "判为盗用后必须撤销全部会话"


# ==================== 撤销即时生效（sid 黑名单） ====================


def test_password_change_revokes_other_devices_immediately(
    anonymous_client, fake_redis
):
    """改密后：当前设备保持可用，其他设备的 access 立即 401（无需等自然过期）。"""
    registered = _register(anonymous_client)
    access_a = registered["access_token"]

    login = anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": registered["user"]["email"], "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    access_b = login.json()["access_token"]

    changed = anonymous_client.post(
        "/api/v1/auth/password/change",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=_hdr(access_a),
    )
    assert changed.status_code == 200, changed.text

    # 当前设备仍可用
    me_a = anonymous_client.get("/api/v1/auth/me", headers=_hdr(access_a))
    assert me_a.status_code == 200, me_a.text
    # 其他设备立即失效
    me_b = anonymous_client.get("/api/v1/auth/me", headers=_hdr(access_b))
    assert me_b.status_code == 401, me_b.text
    assert me_b.json()["code"] == "SESSION_REVOKED"

    # 新密码可登录，旧密码不可
    new_login = anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": registered["user"]["email"], "password": NEW_PASSWORD},
    )
    assert new_login.status_code == 200, new_login.text


# ==================== 设备管理接口 ====================


def test_sessions_endpoint_lists_devices_and_flags_current(anonymous_client):
    registered = _register(anonymous_client)
    access_a = registered["access_token"]
    anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": registered["user"]["email"], "password": PASSWORD},
    )

    listing = anonymous_client.get("/api/v1/auth/sessions", headers=_hdr(access_a))
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["count"] >= 2
    assert len([item for item in body["items"] if item["current"]]) == 1


def test_revoke_specific_device(anonymous_client, fake_redis):
    registered = _register(anonymous_client)
    access_a = registered["access_token"]

    login = anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": registered["user"]["email"], "password": PASSWORD},
    )
    access_b = login.json()["access_token"]

    listing = anonymous_client.get(
        "/api/v1/auth/sessions", headers=_hdr(access_a)
    ).json()
    other = next(item for item in listing["items"] if not item["current"])

    revoked = anonymous_client.delete(
        f"/api/v1/auth/sessions/{other['id']}", headers=_hdr(access_a)
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked"] == 1

    me_b = anonymous_client.get("/api/v1/auth/me", headers=_hdr(access_b))
    assert me_b.status_code == 401
    assert me_b.json()["code"] == "SESSION_REVOKED"


def test_revoke_other_sessions_keeps_current(anonymous_client, fake_redis):
    registered = _register(anonymous_client)
    access_a = registered["access_token"]
    anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": registered["user"]["email"], "password": PASSWORD},
    )

    revoked = anonymous_client.delete("/api/v1/auth/sessions", headers=_hdr(access_a))
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked"] >= 1

    # 当前会话仍在
    listing = anonymous_client.get("/api/v1/auth/sessions", headers=_hdr(access_a))
    assert listing.status_code == 200
    assert listing.json()["count"] == 1
