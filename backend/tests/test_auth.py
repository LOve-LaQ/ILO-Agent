# Tests - 认证与权限边界（真实链路）
"""
阶段 2「权限边界」验收测试：走真实注册 / 登录 / 刷新 / 登出，
真实读写 PostgreSQL，真实签发校验 JWT（不覆盖任何依赖）。

需要 DATABASE_URL 与 JWT_SECRET 已配置（缺任一项整体跳过）。
测试用户邮箱统一以 pytest_ 前缀创建，模块结束后清理。
"""

import uuid

import pytest
from sqlalchemy import delete, select

from src.core.config import settings
from src.core.db import get_session_factory, is_database_configured
from src.models.engagement import UserActivity
from src.models.learning import ChatMessage, LearningSession
from src.models.user import User

pytestmark = pytest.mark.skipif(
    not (is_database_configured() and settings.jwt_secret),
    reason="需要 DATABASE_URL 与 JWT_SECRET（本机 PostgreSQL + backend/.env）",
)

EMAIL_PREFIX = "pytest_"
PASSWORD = "Str0ng-Passw0rd"


def _unique() -> str:
    return uuid.uuid4().hex[:10]


def _register_payload(**overrides) -> dict:
    token = _unique()
    payload = {
        "email": f"{EMAIL_PREFIX}{token}@example.com",
        "username": f"py_{token}",
        "password": PASSWORD,
    }
    payload.update(overrides)
    return payload


def _auth_header(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


@pytest.fixture(scope="module", autouse=True)
def _cleanup_test_users():
    """模块结束后清掉本模块造的测试用户**及其衍生数据**，避免污染开发库。

    只删 users 是不够的：
    - `chat_messages.session_id` 没有外键（临时会话也要能留痕），不会随用户级联删除；
    - `user_activities.user_id` 是 ON DELETE SET NULL，删用户只会把它变成孤儿行。
    （learning_sessions / user_bookmarks 是 CASCADE，随用户一起清掉。）
    """
    yield
    factory = get_session_factory()
    with factory() as session:
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
            session.execute(delete(ChatMessage).where(ChatMessage.session_id.in_(session_ids)))
        session.execute(delete(UserActivity).where(UserActivity.user_id.in_(user_ids)))
        session.execute(delete(User).where(User.id.in_(user_ids)))
        session.commit()


@pytest.fixture(autouse=True)
def _isolated_cookie_jar(anonymous_client):
    """TestClient 是 session 级的，逐个用例清空 Cookie，避免串味"""
    anonymous_client.cookies.clear()
    yield
    anonymous_client.cookies.clear()


@pytest.fixture
def registered(anonymous_client):
    """注册一个用户，返回 (注册请求体, 原始响应, 响应体)"""
    payload = _register_payload()
    response = anonymous_client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201, response.text
    return payload, response, response.json()


# ---------------------------------------------------------------- 注册


def test_register_creates_user_and_logs_in(registered):
    payload, response, body = registered

    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_in"] == settings.access_token_expire_minutes * 60
    assert body["user"]["email"] == payload["email"]
    assert body["user"]["username"] == payload["username"]
    assert body["user"]["is_active"] is True
    # 绝不能把密码或哈希回吐给客户端
    assert "password" not in body["user"]
    assert "password_hash" not in body["user"]

    cookie = response.headers.get("set-cookie", "")
    assert settings.refresh_cookie_name in cookie
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()
    # refresh Cookie 必须限定在认证接口路径下，不能随业务请求自动带上
    assert "Path=/api/v1/auth" in cookie


def test_user_row_is_persisted_with_bcrypt_hash(registered):
    payload, _, body = registered

    factory = get_session_factory()
    with factory() as session:
        user = session.execute(
            select(User).where(User.email == payload["email"])
        ).scalars().one()

    assert str(user.id) == body["user"]["id"]
    assert user.username == payload["username"]
    # 落库的必须是 bcrypt 哈希，绝不是明文
    assert user.password_hash != payload["password"]
    assert user.password_hash.startswith("$2b$")


@pytest.mark.parametrize(
    "overrides,expected_code",
    [
        ({"password": "短"}, "VALIDATION_ERROR"),
        ({"email": "not-an-email"}, "VALIDATION_ERROR"),
        ({"username": "x"}, "VALIDATION_ERROR"),
    ],
)
def test_register_rejects_invalid_input(anonymous_client, overrides, expected_code):
    response = anonymous_client.post(
        "/api/v1/auth/register", json=_register_payload(**overrides)
    )
    assert response.status_code == 422
    assert response.json()["code"] == expected_code


def test_register_rejects_duplicate_email(anonymous_client, registered):
    payload, _, _ = registered
    response = anonymous_client.post(
        "/api/v1/auth/register",
        json=_register_payload(email=payload["email"]),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "EMAIL_TAKEN"


def test_register_rejects_duplicate_username(anonymous_client, registered):
    payload, _, _ = registered
    response = anonymous_client.post(
        "/api/v1/auth/register",
        json=_register_payload(username=payload["username"]),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "USERNAME_TAKEN"


# ---------------------------------------------------------------- 登录 / 刷新 / 登出


def test_login_accepts_email_and_username(anonymous_client, registered):
    payload, _, body = registered

    by_email = anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": payload["email"], "password": PASSWORD},
    )
    assert by_email.status_code == 200, by_email.text
    assert by_email.json()["user"]["id"] == body["user"]["id"]

    by_username = anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": payload["username"], "password": PASSWORD},
    )
    assert by_username.status_code == 200, by_username.text
    assert by_username.json()["user"]["id"] == body["user"]["id"]


def test_login_with_wrong_password_is_401(anonymous_client, registered):
    payload, _, _ = registered
    response = anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": payload["email"], "password": "Wr0ng-Passw0rd"},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_CREDENTIALS"


def test_login_with_unknown_account_is_401(anonymous_client):
    response = anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": f"{EMAIL_PREFIX}ghost@example.com", "password": PASSWORD},
    )
    assert response.status_code == 401
    # 与密码错误同一 code，避免账号枚举
    assert response.json()["code"] == "INVALID_CREDENTIALS"


def test_me_requires_and_accepts_real_token(anonymous_client, registered):
    _, _, body = registered

    anonymous = anonymous_client.get("/api/v1/auth/me")
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "NOT_AUTHENTICATED"

    me = anonymous_client.get("/api/v1/auth/me", headers=_auth_header(body["access_token"]))
    assert me.status_code == 200, me.text
    assert me.json()["id"] == body["user"]["id"]


def test_refresh_returns_new_access_token(anonymous_client, registered):
    """模拟刷新页面：只有 httpOnly Cookie，也能换回可用的 access token"""
    _, _, body = registered

    refreshed = anonymous_client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200, refreshed.text
    new_token = refreshed.json()["access_token"]
    assert new_token

    me = anonymous_client.get("/api/v1/auth/me", headers=_auth_header(new_token))
    assert me.status_code == 200
    assert me.json()["id"] == body["user"]["id"]


def test_refresh_token_cannot_be_used_as_access_token(anonymous_client, registered):
    """refresh token 的 typ 声明必须被校验，否则长效凭证等于永不过期的 access"""
    registered  # 确保已种下 Cookie
    refresh_token = anonymous_client.cookies.get(settings.refresh_cookie_name)
    assert refresh_token

    response = anonymous_client.get(
        "/api/v1/auth/me", headers=_auth_header(refresh_token)
    )
    assert response.status_code == 401
    assert response.json()["code"] == "TOKEN_INVALID"


def test_refresh_without_cookie_is_401(anonymous_client):
    response = anonymous_client.post("/api/v1/auth/refresh")
    assert response.status_code == 401
    assert response.json()["code"] == "REFRESH_TOKEN_MISSING"


def test_logout_clears_refresh_cookie(anonymous_client, registered):
    assert anonymous_client.cookies.get(settings.refresh_cookie_name)

    logout = anonymous_client.post("/api/v1/auth/logout")
    assert logout.status_code == 200
    assert anonymous_client.cookies.get(settings.refresh_cookie_name) is None

    after = anonymous_client.post("/api/v1/auth/refresh")
    assert after.status_code == 401
    assert after.json()["code"] == "REFRESH_TOKEN_MISSING"


# ---------------------------------------------------------------- 端到端串联


def test_real_token_owns_learning_session(anonymous_client, registered):
    """真实 token -> 真实用户 -> 学习会话归属该用户（权限闭环）"""
    _, _, body = registered
    user_id = body["user"]["id"]

    created = anonymous_client.post(
        "/api/v1/learning/session",
        json={"news_item_id": "auth-e2e", "title": "认证后的会话"},
        headers=_auth_header(body["access_token"]),
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]

    from src.api.routes.learning import get_state_machine

    context = get_state_machine().active_sessions[session_id]
    assert context["user_id"] == user_id

    # 请求体里塞别人的 user_id 也抢不走归属
    forged = anonymous_client.post(
        "/api/v1/learning/session",
        json={"news_item_id": "auth-e2e-2", "user_id": "00000000-0000-0000-0000-0000000000ff"},
        headers=_auth_header(body["access_token"]),
    )
    forged_id = forged.json()["session_id"]
    assert get_state_machine().active_sessions[forged_id]["user_id"] == user_id

    # 该用户能正常走完问答
    chat = anonymous_client.post(
        "/api/v1/learning/chat",
        json={"session_id": session_id, "message": "什么是 fixture？"},
        headers=_auth_header(body["access_token"]),
    )
    assert chat.status_code == 200, chat.text
    assert chat.json()["sources"]["topic"] == "认证后的会话"
