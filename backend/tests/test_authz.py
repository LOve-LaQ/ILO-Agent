# Tests - 越权回归（横向权限边界）
"""阶段 5 权限边界验收：确认 A 无法读 / 删 B 的任何资源，且 B 的操作对 A 无副作用。

需要 DATABASE_URL（本机 PostgreSQL + backend/.env），缺则整体跳过。
所有请求都带真实 Bearer token，依赖真实的 `get_current_user`（不覆盖依赖）。
"""

import uuid

import pytest
from sqlalchemy import delete, select

from src.core.db import get_session_factory, is_database_configured
from src.models.engagement import UserActivity
from src.models.learning import ChatMessage, LearningSession
from src.models.user import User

pytestmark = pytest.mark.skipif(
    not is_database_configured(),
    reason="需要 DATABASE_URL（本机 PostgreSQL + backend/.env）",
)

EMAIL_PREFIX = "authz_"
PASSWORD = "Str0ng-Passw0rd"


def _unique() -> str:
    return uuid.uuid4().hex[:10]


def _payload() -> dict:
    token = _unique()
    return {
        "email": f"{EMAIL_PREFIX}{token}@example.com",
        "username": f"authz_{token}",
        "password": PASSWORD,
    }


def _hdr(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _register(client) -> dict:
    resp = client.post("/api/v1/auth/register", json=_payload())
    assert resp.status_code == 201, resp.text
    return resp.json()


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


# ==================== 登录设备会话 ====================


def test_cannot_delete_other_users_session(anonymous_client):
    a = _register(anonymous_client)
    b = _register(anonymous_client)

    a_sessions = anonymous_client.get(
        "/api/v1/auth/sessions", headers=_hdr(a["access_token"])
    ).json()
    a_sid = a_sessions["items"][0]["id"]

    forbidden = anonymous_client.delete(
        f"/api/v1/auth/sessions/{a_sid}", headers=_hdr(b["access_token"])
    )
    assert forbidden.status_code == 404
    assert forbidden.json()["code"] == "SESSION_NOT_FOUND"

    # A 的会话必须毫发无损
    still = anonymous_client.get(
        "/api/v1/auth/sessions", headers=_hdr(a["access_token"])
    ).json()
    assert any(item["id"] == a_sid for item in still["items"])


def test_bulk_revoke_only_affects_own_sessions(anonymous_client, fake_redis):
    a = _register(anonymous_client)
    b = _register(anonymous_client)

    # A 建第二台设备
    anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": a["user"]["email"], "password": PASSWORD},
    )

    # B 下线自己的其他设备：不应波及 A
    anonymous_client.delete("/api/v1/auth/sessions", headers=_hdr(b["access_token"]))

    a_sessions = anonymous_client.get(
        "/api/v1/auth/sessions", headers=_hdr(a["access_token"])
    ).json()
    assert a_sessions["count"] >= 2, "A 的两台设备都应仍在"


# ==================== 学习会话 ====================


def test_cannot_read_other_users_learning_session(anonymous_client):
    a = _register(anonymous_client)
    b = _register(anonymous_client)

    created = anonymous_client.post(
        "/api/v1/learning/session",
        json={"news_item_id": "authz-news", "title": "越权测试主题"},
        headers=_hdr(a["access_token"]),
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]

    resp = anonymous_client.get(
        f"/api/v1/me/sessions/{session_id}", headers=_hdr(b["access_token"])
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "SESSION_NOT_FOUND"

    # 归属者自己仍可读到
    own = anonymous_client.get(
        f"/api/v1/me/sessions/{session_id}", headers=_hdr(a["access_token"])
    )
    assert own.status_code == 200, own.text


# ==================== 收藏 ====================


def test_bookmark_delete_is_scoped_to_owner(anonymous_client):
    a = _register(anonymous_client)
    b = _register(anonymous_client)
    item = "authz-shared-card"

    anonymous_client.post(f"/api/v1/me/bookmarks/{item}", headers=_hdr(a["access_token"]))

    # B 从自己的收藏里删同名卡片：本来就没有，幂等地什么都不变
    removed = anonymous_client.delete(
        f"/api/v1/me/bookmarks/{item}", headers=_hdr(b["access_token"])
    )
    assert removed.status_code == 200
    assert removed.json()["changed"] is False

    # A 的收藏不受影响
    a_list = anonymous_client.get(
        "/api/v1/me/bookmarks", headers=_hdr(a["access_token"])
    ).json()
    assert item in a_list["item_ids"]


def test_bookmarks_are_not_visible_across_users(anonymous_client):
    a = _register(anonymous_client)
    b = _register(anonymous_client)

    anonymous_client.post(
        "/api/v1/me/bookmarks/authz-only-a", headers=_hdr(a["access_token"])
    )

    b_list = anonymous_client.get(
        "/api/v1/me/bookmarks", headers=_hdr(b["access_token"])
    ).json()
    assert b_list["count"] == 0
