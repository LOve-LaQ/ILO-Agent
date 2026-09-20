# Tests - 入口加固（限流 / 登录锁定 / 人机校验 / 密码与用户名策略）
"""阶段 5 入口加固验收：走真实注册 / 登录链路（真实 PostgreSQL，不覆盖依赖）。

需要 DATABASE_URL（本机 PostgreSQL + backend/.env），缺则整体跳过。

- 限流与登录失败计数（login_guard）走注入的 fakeredis，避免污染本机 Redis，
  也让「计数 → 锁定」在单个用例里可确定性复现；
- 人机校验通过在路由命名空间打补丁来模拟「已开启」的行为：
  缺 token → CAPTCHA_REQUIRED，只有约定的 token 视为通过。

保留字用户名的实际返回是 **422 VALIDATION_ERROR**（用户名规范在 pydantic
`model_validator` 里落地），不是 409 —— 409 只留给 EMAIL_TAKEN / USERNAME_TAKEN。
"""

import random
import uuid

import pytest
from sqlalchemy import delete, select

from src.core.config import settings
from src.core.db import get_session_factory, is_database_configured
from src.models.engagement import UserActivity
from src.models.user import User

pytestmark = pytest.mark.skipif(
    not is_database_configured(),
    reason="需要 DATABASE_URL（本机 PostgreSQL + backend/.env）",
)

EMAIL_PREFIX = "hard_"
PASSWORD = "Str0ng-Passw0rd"
GOOD_CAPTCHA = "captcha-ok"


def _unique() -> str:
    return uuid.uuid4().hex[:10]


def _safe_token(length: int = 12) -> str:
    """纯字母、且字母表间隔取字符：不会撞上密码策略里的「顺序字符」判定，
    用于构造「密码与用户名重合」这类需要确定性命中的用例。"""
    return "".join(random.choice("bdfhjlnprtvxz") for _ in range(length))


def _payload(**overrides) -> dict:
    token = _unique()
    payload = {
        "email": f"{EMAIL_PREFIX}{token}@example.com",
        "username": f"hard_{token}",
        "password": PASSWORD,
    }
    payload.update(overrides)
    return payload


def _hdr(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _enable_captcha(monkeypatch) -> None:
    """把「已开启人机校验」的行为装到路由命名空间。

    路由在 `src.api.routes.auth` 里以模块全局名调用 `is_captcha_enabled` /
    `verify_captcha`，因此补丁必须打在**路由模块**上才能被请求读到。
    """
    import src.api.routes.auth as auth_routes
    from src.services.captcha_service import CaptchaError

    monkeypatch.setattr(auth_routes, "is_captcha_enabled", lambda: True)

    def _verify(token, *, request=None, knock=None, dfu=None, ip=None):
        if not token:
            raise CaptchaError("请先完成人机验证。", code="CAPTCHA_REQUIRED")
        if token != GOOD_CAPTCHA:
            raise CaptchaError("人机验证未通过，请重新验证。", code="CAPTCHA_FAILED")

    monkeypatch.setattr(auth_routes, "verify_captcha", _verify)


def _purge() -> None:
    """清掉本模块造的账号及其衍生数据（chat_messages 无外键、activities 是 SET NULL）。"""
    with get_session_factory()() as session:
        user_ids = (
            session.execute(select(User.id).where(User.email.like(f"{EMAIL_PREFIX}%")))
            .scalars()
            .all()
        )
        if not user_ids:
            return
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
    """TestClient 是 session 级的，逐个用例清空 Cookie，避免串味"""
    anonymous_client.cookies.clear()
    yield
    anonymous_client.cookies.clear()


# ==================== 限流 ====================


def test_register_rate_limited_returns_429(monkeypatch, anonymous_client, fake_redis):
    """注册按 IP 限流：超过配额直接 429，且带 Retry-After 供前端提示等待时长。"""
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "register_rate_limit_per_hour", 1)

    first = anonymous_client.post("/api/v1/auth/register", json=_payload())
    assert first.status_code == 201, first.text

    second = anonymous_client.post("/api/v1/auth/register", json=_payload())
    assert second.status_code == 429, second.text
    assert second.json()["code"] == "RATE_LIMITED"
    assert int(second.headers["Retry-After"]) >= 1


# ==================== 人机校验 ====================


def test_register_requires_captcha_when_enabled(monkeypatch, anonymous_client):
    """服务端强制校验：开启后缺 token 一律拒绝（绝不因「前端没传」就放行）。"""
    _enable_captcha(monkeypatch)
    resp = anonymous_client.post("/api/v1/auth/register", json=_payload())
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "CAPTCHA_REQUIRED"


def test_register_rejects_bad_captcha_token(monkeypatch, anonymous_client):
    _enable_captcha(monkeypatch)
    resp = anonymous_client.post(
        "/api/v1/auth/register", json=_payload(captcha_token="not-a-real-token")
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "CAPTCHA_FAILED"


def test_register_accepts_valid_captcha_token(monkeypatch, anonymous_client):
    _enable_captcha(monkeypatch)
    resp = anonymous_client.post(
        "/api/v1/auth/register", json=_payload(captcha_token=GOOD_CAPTCHA)
    )
    assert resp.status_code == 201, resp.text


# ==================== 密码策略 ====================


@pytest.mark.parametrize(
    "weak_password",
    [
        "password1",   # 常见弱密码黑名单
        "12345678",    # 黑名单 + 顺序字符
        "abcd1234",    # 黑名单
        "qwerty123",   # 黑名单
        "aaaaaaaa",    # 连续重复字符
        "P@ssw0rd",    # 黑名单（大小写无关）
    ],
)
def test_register_rejects_weak_password(anonymous_client, weak_password):
    resp = anonymous_client.post(
        "/api/v1/auth/register", json=_payload(password=weak_password)
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_register_rejects_password_matching_identity(anonymous_client):
    """密码与用户名重合必须被拒 —— 这是最容易被猜到的形态之一。"""
    suffix = _safe_token(12)
    username = f"hd{suffix}"
    email = f"{EMAIL_PREFIX}{suffix}@example.com"
    resp = anonymous_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": f"{username}zqzq"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "VALIDATION_ERROR"


# ==================== 用户名规范 ====================


@pytest.mark.parametrize(
    "bad_username",
    [
        "admin",        # 保留字
        "root",         # 保留字
        "official",     # 保留字
        "ilo",          # 保留字（产品名）
        "me",           # 保留字（撞路由）
        "支持",          # 非 ASCII 字符集
        "a b",          # 含空格
        "ab__cd",       # 连续分隔符
        "_ab",          # 前导下划线
        "ab-",          # 结尾连字符
    ],
)
def test_register_rejects_invalid_username(anonymous_client, bad_username):
    resp = anonymous_client.post(
        "/api/v1/auth/register", json=_payload(username=bad_username)
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "VALIDATION_ERROR"


# ==================== 登录失败锁定 ====================


def test_login_lockout_message_hides_account_existence(
    monkeypatch, anonymous_client, fake_redis
):
    """连续失败触发锁定，但话术绝不能暴露账号是否存在。

    对「存在的账号」与「不存在的账号」做同样次数的失败尝试后，锁定响应必须
    完全一致 —— 否则登录接口就成了账号枚举器。
    """
    monkeypatch.setattr(settings, "login_max_failures", 3)
    monkeypatch.setattr(settings, "login_lockout_minutes", 15)

    payload = _payload()
    registered = anonymous_client.post("/api/v1/auth/register", json=payload)
    assert registered.status_code == 201, registered.text

    known = payload["email"]
    unknown = f"{EMAIL_PREFIX}ghost@example.com"

    for _ in range(3):
        resp = anonymous_client.post(
            "/api/v1/auth/login",
            json={"identifier": known, "password": "Wr0ng-Passw0rd"},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "INVALID_CREDENTIALS"

    locked_known = anonymous_client.post(
        "/api/v1/auth/login", json={"identifier": known, "password": "Wr0ng-Passw0rd"}
    )
    assert locked_known.status_code == 429, locked_known.text
    assert locked_known.json()["code"] == "RATE_LIMITED"
    assert int(locked_known.headers["Retry-After"]) >= 1

    for _ in range(3):
        anonymous_client.post(
            "/api/v1/auth/login",
            json={"identifier": unknown, "password": "Wr0ng-Passw0rd"},
        )
    locked_unknown = anonymous_client.post(
        "/api/v1/auth/login", json={"identifier": unknown, "password": "Wr0ng-Passw0rd"}
    )

    # 存在与不存在：状态码与话术必须一字不差
    assert locked_unknown.status_code == locked_known.status_code == 429
    assert locked_unknown.json()["message"] == locked_known.json()["message"]

    message = locked_known.json()["message"]
    assert "频繁" in message
    assert "锁定" not in message
    assert "不存在" not in message


def test_login_success_clears_failure_counter(monkeypatch, anonymous_client, fake_redis):
    """登录成功即清零：否则历史失败会一直累积到误触发锁定。"""
    monkeypatch.setattr(settings, "login_max_failures", 3)

    payload = _payload()
    anonymous_client.post("/api/v1/auth/register", json=payload)

    for _ in range(2):
        anonymous_client.post(
            "/api/v1/auth/login",
            json={"identifier": payload["email"], "password": "Wr0ng-Passw0rd"},
        )
    # 未达阈值，正确密码仍可登录
    ok = anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": payload["email"], "password": PASSWORD},
    )
    assert ok.status_code == 200, ok.text

    # 清零后再错两次不会立刻锁定（第三个请求仍是 401 而非 429）
    for _ in range(2):
        anonymous_client.post(
            "/api/v1/auth/login",
            json={"identifier": payload["email"], "password": "Wr0ng-Passw0rd"},
        )
    still_401 = anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": payload["email"], "password": "Wr0ng-Passw0rd"},
    )
    assert still_401.status_code == 401, still_401.text


# ==================== 登录的人机校验时机 ====================


def test_login_requires_captcha_only_after_failure(
    monkeypatch, anonymous_client, fake_redis
):
    """正常用户不该被每次登录打扰：只有该账号已有失败记录时才要求人机校验。"""
    _enable_captcha(monkeypatch)
    monkeypatch.setattr(settings, "login_max_failures", 10)

    payload = _payload()
    registered = anonymous_client.post(
        "/api/v1/auth/register",
        json={**payload, "captcha_token": GOOD_CAPTCHA},
    )
    assert registered.status_code == 201, registered.text

    # 首次登录（尚无失败记录）：不要求人机校验，直接走到密码校验
    first = anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": payload["email"], "password": "Wr0ng-Passw0rd"},
    )
    assert first.status_code == 401
    assert first.json()["code"] == "INVALID_CREDENTIALS"

    # 已有失败记录：缺 captcha token 必须被拒
    second = anonymous_client.post(
        "/api/v1/auth/login",
        json={"identifier": payload["email"], "password": "Wr0ng-Passw0rd"},
    )
    assert second.status_code == 400
    assert second.json()["code"] == "CAPTCHA_REQUIRED"

    # 带上有效 captcha 放行到密码校验（密码仍错）
    third = anonymous_client.post(
        "/api/v1/auth/login",
        json={
            "identifier": payload["email"],
            "password": "Wr0ng-Passw0rd",
            "captcha_token": GOOD_CAPTCHA,
        },
    )
    assert third.status_code == 401
    assert third.json()["code"] == "INVALID_CREDENTIALS"

    # 正确密码 + captcha 登录成功，并记录访问
    ok = anonymous_client.post(
        "/api/v1/auth/login",
        json={
            "identifier": payload["email"],
            "password": payload["password"],
            "captcha_token": GOOD_CAPTCHA,
        },
    )
    assert ok.status_code == 200, ok.text
    me = anonymous_client.get(
        "/api/v1/auth/me", headers=_hdr(ok.json()["access_token"])
    )
    assert me.status_code == 200


# ==================== 改密的密码策略（路由级身份校验） ====================


def test_password_change_rejects_password_matching_identity(anonymous_client):
    """改密能拿到用户名/邮箱，因此做一次「密码不得与身份重合」的完整校验（400）。"""
    payload = _payload()
    registered = anonymous_client.post("/api/v1/auth/register", json=payload)
    access = registered.json()["access_token"]

    new_password = f"{payload['username']}zqzq"
    resp = anonymous_client.post(
        "/api/v1/auth/password/change",
        json={"current_password": PASSWORD, "new_password": new_password},
        headers=_hdr(access),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "WEAK_PASSWORD"


# ==================== 启动期安全自检（人机校验） ====================


def test_production_with_captcha_off_refuses_to_start(monkeypatch):
    """生产环境配 CAPTCHA_PROVIDER=off：必须拒绝启动。

    这是最隐蔽的一条误配路径 —— 没有告警、也没有「缺 key」那种线索，注册入口
    就是彻底裸奔。所以它必须在启动时 fail-closed，而不是等第一次注册才发现。
    """
    from src.services import captcha_service

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "captcha_provider", "off")

    with pytest.raises(RuntimeError, match="off"):
        captcha_service.assert_security_config()


def test_development_still_allows_captcha_off(monkeypatch):
    """非生产环境仍允许 off —— 本地不配 VAPTCHA 也要能完整跑通，不能误伤。"""
    from src.services import captcha_service

    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "captcha_provider", "off")

    captcha_service.assert_security_config()  # 不应抛异常
