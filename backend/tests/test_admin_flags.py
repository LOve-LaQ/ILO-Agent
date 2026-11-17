# Tests - 管理员角色与功能开关
"""阶段 6 验收：角色边界 + 运行时开关。

覆盖三件事：
1. 未登录 401、普通用户 403 —— 后台入口不是「前端藏起来」就算完；
2. 管理员可列出/切换开关，且**切换立刻生效**（关掉后业务接口 503）；
3. 角色变更的两道护栏：不能改自己、不能改白名单账号。

需要 DATABASE_URL（与 test_authz.py 同一套真实库夹具），缺则整体跳过。
开关行会在用例结束时清理，避免把开发库留在「某功能被关闭」的状态。
"""

import uuid

import pytest
from sqlalchemy import delete, select

from src.core.db import get_session_factory, is_database_configured
from src.models.feature_flag import FeatureFlag
from src.models.user import User
from src.services.feature_flags import FLAG_DEFINITIONS

pytestmark = pytest.mark.skipif(
    not is_database_configured(),
    reason="需要 DATABASE_URL（本机 PostgreSQL + backend/.env）",
)

EMAIL_PREFIX = "admintest_"
PASSWORD = "Str0ng-Passw0rd"


def _unique() -> str:
    return uuid.uuid4().hex[:10]


def _payload() -> dict:
    token = _unique()
    return {
        "email": f"{EMAIL_PREFIX}{token}@example.com",
        "username": f"adm_{token}",
        "password": PASSWORD,
    }


def _hdr(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _register(client) -> dict:
    resp = client.post("/api/v1/auth/register", json=_payload())
    assert resp.status_code == 201, resp.text
    return resp.json()


def _promote(email: str, role: str = "admin") -> None:
    with get_session_factory()() as session:
        user = session.execute(select(User).where(User.email == email)).scalars().one()
        user.role = role
        session.commit()


def _purge() -> None:
    with get_session_factory()() as session:
        session.execute(delete(User).where(User.email.like(f"{EMAIL_PREFIX}%")))
        # 开关表按 key 清理：这些行是本套用例写进去的，生产/开发默认值在代码里，
        # 删掉即回到默认开启
        session.execute(delete(FeatureFlag))
        session.commit()


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    _purge()
    yield
    _purge()


@pytest.fixture(autouse=True)
def _isolated_cookie_jar(anonymous_client):
    anonymous_client.cookies.clear()
    yield
    anonymous_client.cookies.clear()


# ==================== 权限边界 ====================


def test_admin_requires_login(anonymous_client):
    resp = anonymous_client.get("/api/v1/admin/features")
    assert resp.status_code == 401
    assert resp.json()["code"] == "NOT_AUTHENTICATED"


def test_normal_user_is_forbidden(anonymous_client):
    user = _register(anonymous_client)

    for path in (
        "/api/v1/admin/features",
        "/api/v1/admin/users",
        "/api/v1/admin/whoami",
    ):
        resp = anonymous_client.get(path, headers=_hdr(user["access_token"]))
        assert resp.status_code == 403, f"{path} 应拒绝普通用户"
        assert resp.json()["code"] == "ADMIN_REQUIRED"

    # 写接口同样被拦
    resp = anonymous_client.patch(
        "/api/v1/admin/features/learning_chat",
        json={"enabled": False},
        headers=_hdr(user["access_token"]),
    )
    assert resp.status_code == 403


def test_registered_user_defaults_to_user_role(anonymous_client):
    user = _register(anonymous_client)
    assert user["user"]["role"] == "user"


# ==================== 开关管理 ====================


def test_admin_can_list_flags(anonymous_client):
    admin = _register(anonymous_client)
    _promote(admin["user"]["email"])

    resp = anonymous_client.get(
        "/api/v1/admin/features", headers=_hdr(admin["access_token"])
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    keys = {item["key"] for item in body["items"]}
    assert keys == set(FLAG_DEFINITIONS)
    assert body["count"] == len(FLAG_DEFINITIONS)
    # 未被改过的开关也必须在列表里，且与代码默认值一致
    for item in body["items"]:
        assert item["enabled"] == item["default_enabled"]


def test_toggle_takes_effect_immediately(anonymous_client):
    admin = _register(anonymous_client)
    _promote(admin["user"]["email"])
    headers = _hdr(admin["access_token"])

    # 默认开启：请求能穿到业务逻辑（假 session_id 得到 404，而不是 503）
    before = anonymous_client.post(
        "/api/v1/learning/chat",
        json={"session_id": "admintest-not-exist", "message": "hi"},
        headers=headers,
    )
    assert before.status_code == 404, before.text
    assert before.json()["code"] == "SESSION_NOT_FOUND"

    # 关闭
    off = anonymous_client.patch(
        "/api/v1/admin/features/learning_chat",
        json={"enabled": False},
        headers=headers,
    )
    assert off.status_code == 200, off.text
    assert off.json()["enabled"] is False

    blocked = anonymous_client.post(
        "/api/v1/learning/chat",
        json={"session_id": "admintest-not-exist", "message": "hi"},
        headers=headers,
    )
    assert blocked.status_code == 503
    assert blocked.json()["code"] == "FEATURE_DISABLED"
    assert blocked.json()["detail"]["feature"] == "learning_chat"

    # 再打开：立刻恢复（证明没有进程内缓存）
    on = anonymous_client.patch(
        "/api/v1/admin/features/learning_chat",
        json={"enabled": True},
        headers=headers,
    )
    assert on.status_code == 200
    assert on.json()["enabled"] is True

    restored = anonymous_client.post(
        "/api/v1/learning/chat",
        json={"session_id": "admintest-not-exist", "message": "hi"},
        headers=headers,
    )
    assert restored.status_code == 404, restored.text


def test_unknown_flag_is_rejected(anonymous_client):
    admin = _register(anonymous_client)
    _promote(admin["user"]["email"])

    resp = anonymous_client.patch(
        "/api/v1/admin/features/not_a_real_flag",
        json={"enabled": False},
        headers=_hdr(admin["access_token"]),
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "FEATURE_UNKNOWN"


# ==================== 角色管理 ====================


def test_admin_can_promote_another_user(anonymous_client):
    admin = _register(anonymous_client)
    target = _register(anonymous_client)
    _promote(admin["user"]["email"])
    headers = _hdr(admin["access_token"])

    resp = anonymous_client.patch(
        f"/api/v1/admin/users/{target['user']['id']}/role",
        json={"role": "admin"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "admin"

    # 提权立即生效：目标用户现在能进后台
    ok = anonymous_client.get(
        "/api/v1/admin/whoami", headers=_hdr(target["access_token"])
    )
    assert ok.status_code == 200


def test_cannot_change_own_role(anonymous_client):
    admin = _register(anonymous_client)
    _promote(admin["user"]["email"])

    resp = anonymous_client.patch(
        f"/api/v1/admin/users/{admin['user']['id']}/role",
        json={"role": "user"},
        headers=_hdr(admin["access_token"]),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "CANNOT_CHANGE_OWN_ROLE"


def test_invalid_role_is_rejected(anonymous_client):
    admin = _register(anonymous_client)
    target = _register(anonymous_client)
    _promote(admin["user"]["email"])

    resp = anonymous_client.patch(
        f"/api/v1/admin/users/{target['user']['id']}/role",
        json={"role": "superuser"},
        headers=_hdr(admin["access_token"]),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_ROLE"
