# Tests - pytest 公共夹具
"""
把 backend/ 注入 sys.path，使测试内可统一使用 `src.*` 前缀导入
（与 src/api/main.py 的导入前缀保持一致，避免同一文件被加载成两个模块）。

权限边界相关的夹具：
- 默认**注入固定测试用户**（覆盖 `get_current_user`），让 learning/* 的契约测试
  不依赖真实 PostgreSQL；
- 需要验证 401 的用例改用 `anonymous_client`（不覆盖依赖）；
- 真实注册/登录链路另有 tests/test_auth.py，缺 DATABASE_URL 时自动跳过。
"""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.deps import get_current_user  # noqa: E402
from src.api.main import app  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.models.user import User  # noqa: E402

TEST_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TEST_USERNAME = "pytest-user"


def _fake_current_user() -> User:
    """不落库的测试用户（路由只读 id / is_active 等属性）"""
    return User(
        id=TEST_USER_ID,
        email="pytest@example.com",
        username=TEST_USERNAME,
        password_hash="$2b$12$placeholder",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture(scope="session")
def raw_client() -> TestClient:
    """应用级 TestClient"""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def client(raw_client: TestClient) -> TestClient:
    """默认已登录的客户端（别名，便于阅读）"""
    return raw_client


@pytest.fixture(scope="session")
def test_user_id() -> uuid.UUID:
    """被注入的测试用户 id（断言会话归属时用）"""
    return TEST_USER_ID


@pytest.fixture(scope="session", autouse=True)
def _authenticated_by_default():
    """默认注入测试用户，使 learning/* 契约测试不依赖数据库"""
    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def anonymous_client(raw_client: TestClient):
    """未登录客户端：临时摘掉依赖覆盖，用于验证 401 / 403"""
    app.dependency_overrides.pop(get_current_user, None)
    yield raw_client
    app.dependency_overrides[get_current_user] = _fake_current_user


@pytest.fixture(autouse=True)
def _force_llm_fallback(monkeypatch):
    """契约测试不打真实 LLM/外部网络：强制走内置降级回答，保证离线可复现。

    路由在调用时才 `from src.modules.agent.state_machine import llm, LLM_AVAILABLE`，
    因此此处打补丁可以被后续请求读到。
    """
    from src.modules.agent import state_machine as sm_module

    monkeypatch.setattr(sm_module, "LLM_AVAILABLE", False, raising=False)
    monkeypatch.setattr(sm_module, "llm", None, raising=False)


# 契约测试注入的是**数据库里并不存在的假用户**，落库本该整体失败；但阶段 3 引入
# 落库后暴露出两个漏口：
# - `chat_messages.session_id` 没有外键（临时会话也要能留痕），行会真的写进去；
# - `user_activities.user_id` 是 ON DELETE SET NULL，删用户后只留下孤儿行。
# 结果是每跑一次 pytest 就往真实开发库写脏数据。这里直接把「数据库可用」关掉，
# 让所有 best-effort 落库退化为 no-op，契约测试回到纯内存语义。
# 走真实读写链路的 test_auth.py 必须放行。
_DB_CONSUMER_MODULES = (
    "src.services.activity_service",
    "src.services.bookmark_service",
    "src.services.collection_service",
    # 兴趣画像会查用户行为表：契约测试里注的是假用户，真查库既慢又没意义，
    # 必须一并关掉（否则 /discover/news 的个性化分支会真的去连开发库）
    "src.services.interest_profile",
    "src.services.learning_service",
)
_REAL_DB_TEST_MODULES = {
    "test_auth.py",
    "test_me_routes.py",
    "test_auth_hardening.py",
    "test_session_security.py",
    "test_password_reset.py",
    "test_account_deletion.py",
    "test_authz.py",
    "test_email_verification.py",
}


@pytest.fixture(scope="session", autouse=True)
def _disable_security_guards_for_session():
    """会话级先把限流与人机校验关掉（用直接赋值，不是 monkeypatch）。

    为什么不能只靠函数级的 `_relax_security_guards`：**模块级**夹具（如
    test_me_routes 的 accounts、或本套新增用例的模块级账号夹具）会在函数级夹具
    之前建立并直接发请求；此时若本机 Redis 里攒着计数，注册会先撞 429，整个模块
    在 setup 阶段就崩了。会话级夹具在所有模块级夹具之前运行，先把全局开关置为关闭，
    函数级夹具再按用例需要临时打开。这里不还原：会话级开关留到进程结束即可。
    """
    settings.rate_limit_enabled = False
    settings.captcha_provider = "off"
    yield


@pytest.fixture(autouse=True)
def _relax_security_guards(monkeypatch):
    """逐个用例关闭限流与人机校验，避免真实 Redis / 外部网络让用例互相干扰。

    限流是「按 IP / 账号计数」的，契约测试集中从同一来源发请求，若不关闭会
    直接撞上限额（429）而误判失败；人机校验同理（测试环境没有真实 token）。
    需要验证加固行为的用例自行 monkeypatch 打开，并注入 fakeredis。
    """
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "captcha_provider", "off")
    yield


@pytest.fixture
def fake_redis():
    """注入 fakeredis：限流 / 登录失败计数 / sid 黑名单全部走内存，用例间互不干扰。

    覆盖「需要 Redis 可用」的安全行为（例如撤销会话后 access 立即失效）时使用；
    退出时还原为「未注入」，让后续用例重新探测真实 Redis，避免污染本机数据。
    """
    import fakeredis

    from src.core import redis_client

    client = fakeredis.FakeRedis(decode_responses=True)
    redis_client.set_redis_client(client)
    yield client
    redis_client.reset_redis_client()


@pytest.fixture(autouse=True)
def _isolate_database(request, monkeypatch):
    """契约测试不触碰真实开发库（test_auth.py 例外）"""
    if Path(str(request.node.fspath)).name in _REAL_DB_TEST_MODULES:
        yield
        return

    import importlib

    for module_name in _DB_CONSUMER_MODULES:
        module = importlib.import_module(module_name)
        # 各服务在导入时就把该函数绑定到了自己的命名空间，
        # 所以补丁必须打在**服务模块**上，打 src.core.db 是无效的。
        monkeypatch.setattr(module, "is_database_configured", lambda: False)
    yield
