# Tests - API 契约边界
"""
阶段 1「契约边界」验收测试。

覆盖三件事：
1. OpenAPI 里每个接口都声明了明确的 2xx 响应 schema —— 前端 `npm run gen:api`
   才能派生类型，`/docs` 也才无空洞；
2. 统一错误契约 {code, message, detail} 在 400/404/409/422 各分支真实生效；
3. discover / learning 主链路的真实往返（离线可复现，见 conftest 的 LLM 降级夹具）。

运行：cd backend && .\\ilo\\Scripts\\python.exe -m pytest tests -v
"""

import pytest

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
CATEGORY_CODES = {
    "backend",
    "frontend",
    "ai_ml",
    "devops",
    "database",
    "mobile",
    "security",
    "tools",
    "other",
}


def _operations(openapi: dict):
    """遍历 OpenAPI 中的所有 (path, method, operation)"""
    for path, item in openapi["paths"].items():
        for method, operation in item.items():
            if method in HTTP_METHODS:
                yield path, method, operation


@pytest.fixture(scope="module")
def openapi(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    return response.json()


def _create_session(client, item_id: str = "test-item-1", topic: str = "pytest 契约测试主题") -> str:
    response = client.post(
        "/api/v1/learning/session",
        json={
            "news_item_id": item_id,
            "title": topic,
            "summary": "用于验证学习会话契约",
            "core_concepts": ["fixture", "断言"],
            "time_budget": 15,
            "preferred_depth": "medium",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "created"
    assert set(body["context"]) == {"topic", "time_budget_minutes", "current_state"}
    return body["session_id"]


# ---------------------------------------------------------------- 契约完整性


def test_every_operation_declares_success_schema(openapi):
    """每个接口的 2xx 响应都必须有明确 schema，不允许出现 unknown / 空 schema"""
    missing = []
    for path, method, operation in _operations(openapi):
        success = {
            status: response
            for status, response in operation["responses"].items()
            if status.isdigit() and 200 <= int(status) < 300
        }
        schema = None
        for response in success.values():
            schema = response.get("content", {}).get("application/json", {}).get("schema")
            if schema:
                break
        if not schema:
            missing.append(f"{method.upper()} {path}")
    assert not missing, f"以下接口缺少 2xx 响应 schema: {missing}"


def test_every_business_operation_declares_error_contract(openapi):
    """业务接口必须把统一错误信封写进契约，前端才有类型可用"""
    schemas = openapi["components"]["schemas"]
    assert "ErrorResponse" in schemas, "ErrorResponse 未进入 OpenAPI"
    assert set(schemas["ErrorResponse"]["required"]) == {"code", "message"}

    missing = []
    for path, method, operation in _operations(openapi):
        if not path.startswith("/api/v1/"):
            continue
        for status in ("400", "401", "403", "404", "422", "500"):
            if status not in operation["responses"]:
                missing.append(f"{method.upper()} {path} -> {status}")
    assert not missing, f"以下接口缺少错误响应声明: {missing}"


def test_card_contract_fields_are_typed(openapi):
    """TechCard 必须是收敛后的字段集，不能再出现 created_at / updated_at 双命名"""
    tech_card = openapi["components"]["schemas"]["TechCard"]
    properties = tech_card["properties"]
    assert "created_at" not in properties
    assert "updated_at" not in properties
    assert "published_at" in properties
    assert "source_updated_at" in properties
    # type / category 必须是受控枚举，而不是自由字符串
    assert tech_card["properties"]["type"]["enum"] == ["repo", "article"]
    assert set(tech_card["properties"]["category"]["enum"]) == CATEGORY_CODES


# ---------------------------------------------------------------- discover


def test_news_returns_card_list_contract(client):
    response = client.get("/api/v1/discover/news", params={"limit": 3})
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) >= {"items", "count", "total", "has_more", "source"}
    assert body["count"] == len(body["items"])
    assert body["count"] <= 3

    for card in body["items"]:
        assert isinstance(card["id"], str) and card["id"]
        assert isinstance(card["title"], str) and card["title"]
        assert card["type"] in {"repo", "article"}
        assert card["category"] in CATEGORY_CODES


def test_articles_keep_contract_and_degrade_gracefully(client):
    """文章接口不得再抛 503：知识库为空时回退内置示例并用 source 标识"""
    response = client.get("/api/v1/discover/articles", params={"limit": 3})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["source"] in {"knowledge_base", "sample"}
    if body["source"] == "sample":
        assert body["count"] > 0, "降级到 sample 时不应返回空列表"
    assert all(card["type"] == "article" for card in body["items"])
    assert all(card["category"] in CATEGORY_CODES for card in body["items"])


def test_by_tag_returns_tagged_response(client):
    response = client.get("/api/v1/discover/by-tag/python")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tag"] == "python"
    assert set(body) == {"tag", "items", "count"}


# ---------------------------------------------------------------- 错误契约


def test_validation_error_uses_error_envelope(client):
    response = client.post("/api/v1/learning/complete", json={})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["message"]
    assert "detail" in body


def test_unknown_session_returns_404_envelope(client):
    response = client.post("/api/v1/learning/complete", json={"session_id": "sess_not_exist"})
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "SESSION_NOT_FOUND"
    assert "重新打开卡片" in body["message"]


def test_quiz_before_explanation_returns_409(client):
    session_id = _create_session(client, item_id="quiz-not-ready")
    response = client.post(
        "/api/v1/learning/quiz", json={"session_id": session_id, "user_answers": [0]}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "QUIZ_NOT_READY"


def test_missing_message_returns_400_envelope(client):
    response = client.post("/api/v1/learning/chat", json={"message": "   "})
    assert response.status_code == 400
    assert response.json()["code"] == "MESSAGE_REQUIRED"


# ---------------------------------------------------------------- learning 主链路


def test_session_chat_complete_lifecycle(client):
    session_id = _create_session(client, item_id="lifecycle-item")

    chat = client.post(
        "/api/v1/learning/chat",
        json={
            "session_id": session_id,
            "message": "什么是 fixture？",
            "conversation_history": [{"role": "user", "content": "你好"}],
        },
    )
    assert chat.status_code == 200, chat.text
    chat_body = chat.json()
    assert chat_body["session_id"] == session_id
    assert chat_body["response"]
    # sources 必须回填真实会话上下文，而非硬编码占位
    assert chat_body["sources"]["topic"] == "pytest 契约测试主题"
    assert len(chat_body["conversation_history"]) == 3

    done = client.post("/api/v1/learning/complete", json={"session_id": session_id})
    assert done.status_code == 200, done.text
    done_body = done.json()
    assert done_body["status"] == "completed"
    assert done_body["topic"] == "pytest 契约测试主题"
    assert done_body["elapsed_minutes"] >= 0

    # 会话结束后应被清理，再次操作返回 404 而不是 500
    again = client.post("/api/v1/learning/complete", json={"session_id": session_id})
    assert again.status_code == 404
    assert again.json()["code"] == "SESSION_NOT_FOUND"


def test_push_and_response_use_real_session_context(client):
    topic = "pytest 推送上下文主题"
    session_id = _create_session(client, item_id="push-item", topic=topic)

    push = client.post("/api/v1/learning/push", json={"session_id": session_id})
    assert push.status_code == 200, push.text
    push_body = push.json()
    assert push_body["state"] == "pushed"
    assert topic in push_body["message"]
    assert push_body["actions"]

    learn = client.post(
        "/api/v1/learning/response", json={"session_id": session_id, "user_input": "展开讲讲"}
    )
    assert learn.status_code == 200, learn.text
    learn_body = learn.json()
    assert learn_body["state"] == "quiz"
    assert learn_body["explanation"]
    assert len(learn_body["quiz"]["questions"]) == learn_body["quiz"]["total"]


def test_quiz_response_shape(client):
    session_id = _create_session(client, item_id="quiz-item")
    learn = client.post(
        "/api/v1/learning/response", json={"session_id": session_id, "user_input": "展开讲讲"}
    )
    questions = learn.json()["quiz"]["questions"]

    response = client.post(
        "/api/v1/learning/quiz",
        json={
            "session_id": session_id,
            "user_answers": [q["correct_answer"] for q in questions],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "fsrs_update"
    assert body["score"] == 1.0
    assert len(body["explanations"]) == len(questions)
    assert body["review_data"]["new_interval"] > 0


def test_learning_health_contract(client):
    response = client.get("/api/v1/learning/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert isinstance(body["state_machine_ready"], bool)


# ---------------------------------------------------------------- 根路由


def test_root_and_health_contracts(client):
    root = client.get("/")
    assert root.status_code == 200
    assert set(root.json()) == {"service", "version", "status", "docs", "health"}

    health = client.get("/health")
    assert health.status_code == 200
    assert set(health.json()) == {"status", "service"}


# ---------------------------------------------------------------- 权限边界


PROTECTED_CALLS = [
    ("post", "/api/v1/learning/session", {"news_item_id": "x"}),
    ("post", "/api/v1/learning/push", {"session_id": "sess_x"}),
    ("post", "/api/v1/learning/chat", {"message": "hi"}),
    ("post", "/api/v1/learning/response", {"session_id": "sess_x", "user_input": "讲讲"}),
    ("post", "/api/v1/learning/quiz", {"session_id": "sess_x", "user_answers": []}),
    ("post", "/api/v1/learning/complete", {"session_id": "sess_x"}),
    ("get", "/api/v1/auth/me", None),
    # 个人中心：全部只看 current_user 自己的数据，未登录必须 401
    ("get", "/api/v1/me/profile", None),
    ("get", "/api/v1/me/bookmarks", None),
    ("post", "/api/v1/me/bookmarks/article-001", None),
    ("delete", "/api/v1/me/bookmarks/article-001", None),
    ("get", "/api/v1/me/sessions", None),
    ("get", "/api/v1/me/sessions/sess_x", None),
    ("get", "/api/v1/me/activities", None),
]


@pytest.mark.parametrize(
    "method,path,payload", PROTECTED_CALLS, ids=[f"{m} {p}" for m, p, _ in PROTECTED_CALLS]
)
def test_protected_endpoints_require_login(anonymous_client, method, path, payload):
    """未登录访问受保护接口必须是 401（而不是 500 或静默放行）"""
    send = getattr(anonymous_client, method)
    response = send(path, json=payload) if payload is not None else send(path)
    assert response.status_code == 401, f"{method.upper()} {path} -> {response.status_code}"
    body = response.json()
    assert body["code"] in {"NOT_AUTHENTICATED", "TOKEN_INVALID", "USER_NOT_FOUND"}
    assert body["message"]


def test_discover_and_learning_health_stay_anonymous(anonymous_client):
    """边界另一侧：资讯浏览与健康探针不要求登录"""
    news = anonymous_client.get("/api/v1/discover/news", params={"limit": 1})
    assert news.status_code == 200
    health = anonymous_client.get("/api/v1/learning/health")
    assert health.status_code == 200


def test_session_owner_comes_from_token_not_request_body(client, test_user_id):
    """会话归属者取自 access token，请求体里的 user_id 不再被信任"""
    response = client.post(
        "/api/v1/learning/session",
        json={"news_item_id": "owner-check", "user_id": "attacker-supplied-id"},
    )
    assert response.status_code == 200, response.text
    session_id = response.json()["session_id"]

    from src.api.routes.learning import get_state_machine

    context = get_state_machine().active_sessions[session_id]
    assert context["user_id"] == str(test_user_id)


def test_another_users_session_is_forbidden(client):
    """拿到别人的 session_id 也不能读其内容"""
    created = client.post(
        "/api/v1/learning/session", json={"news_item_id": "cross-user-check"}
    )
    session_id = created.json()["session_id"]

    from src.api.routes.learning import get_state_machine

    get_state_machine().active_sessions[session_id]["user_id"] = "someone-else"

    response = client.post("/api/v1/learning/complete", json={"session_id": session_id})
    assert response.status_code == 403
    assert response.json()["code"] == "SESSION_FORBIDDEN"


def test_auth_contract_is_exposed(openapi):
    """认证接口必须进入契约，且 refresh 走 httpOnly Cookie"""
    paths = openapi["paths"]
    assert "/api/v1/auth/register" in paths
    assert "/api/v1/auth/login" in paths
    assert "/api/v1/auth/refresh" in paths
    assert "/api/v1/auth/logout" in paths
    assert "/api/v1/auth/me" in paths

    session_schema = openapi["components"]["schemas"]["SessionCreateRequest"]
    assert "user_id" not in session_schema["properties"], (
        "SessionCreateRequest 不应包含 user_id —— 身份只能来自 token"
    )


def test_me_and_provenance_contracts_are_exposed(openapi):
    """个人中心与内容溯源必须进入契约，前端才能据此生成类型"""
    paths = openapi["paths"]
    for path in (
        "/api/v1/me/profile",
        "/api/v1/me/bookmarks",
        "/api/v1/me/bookmarks/{item_id}",
        "/api/v1/me/sessions",
        "/api/v1/me/sessions/{session_id}",
        "/api/v1/me/activities",
        "/api/v1/discover/cards/{card_id}/provenance",
    ):
        assert path in paths, f"契约缺少 {path}"


def test_provenance_is_readable_anonymously(anonymous_client):
    """内容溯源是公开的卡片元数据：匿名可读，且没有记录时也照常返回卡片"""
    response = anonymous_client.get("/api/v1/discover/cards/article-001/provenance")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["known"] is False
    assert body["card"] is not None
