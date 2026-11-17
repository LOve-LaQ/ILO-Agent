"""单端口同源托管（SERVE_FRONTEND）的契约。

内网穿透演示只暴露一个端口，前端必须由后端托管。这里锁定四条边界：
根路径给页面、前端路由回退 index.html、API 的 404 不被 SPA 吞掉、
不存在的静态资源必须真 404（回退 HTML 会让浏览器把它当 JS 解析）。
"""

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_application
from src.core.config import BACKEND_ROOT, settings

DIST = BACKEND_ROOT.parent / "frontend" / "dist"

pytestmark = pytest.mark.skipif(
    not (DIST / "index.html").is_file(),
    reason="前端未构建（缺少 frontend/dist/index.html）",
)


@pytest.fixture
def spa_client(monkeypatch):
    """开启同源托管的客户端（不进入 with，避免触发 lifespan 预热）"""
    monkeypatch.setattr(settings, "serve_frontend", True)
    monkeypatch.setattr(settings, "frontend_dist", str(DIST))
    return TestClient(create_application())


def test_root_serves_index_html(spa_client: TestClient):
    response = spa_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<!doctype html" in response.text.lower()


def test_frontend_route_falls_back_to_index(spa_client: TestClient):
    """SPA 深链（刷新 /learn/xxx）必须交出 index.html，而不是 404"""
    response = spa_client.get("/learn/abc-123")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_service_info_moved_off_root(spa_client: TestClient):
    """根路径已让给前端，服务信息改由 /api/v1/info 提供"""
    body = spa_client.get("/api/v1/info").json()
    assert set(body) == {"service", "version", "status", "docs", "health"}
    assert body["service"] == "ILO-Agent Demo"


def test_unknown_api_path_stays_404(spa_client: TestClient):
    """API 的 404 绝不能回退成 200 + HTML"""
    response = spa_client.get("/api/v1/not-a-real-route")
    assert response.status_code == 404
    assert "application/json" in response.headers["content-type"]


def test_missing_asset_returns_404(spa_client: TestClient):
    """带扩展名的资源找不到就该 404：回 HTML 会报 Unexpected token '<'"""
    response = spa_client.get("/assets/not-exist-abc123.js")
    assert response.status_code == 404


def test_disabled_mode_keeps_json_root(monkeypatch):
    """未开启托管时保持原开发契约：/ 返回服务信息 JSON"""
    monkeypatch.setattr(settings, "serve_frontend", False)
    body = TestClient(create_application()).get("/").json()
    assert body["service"] == "ILO-Agent Demo"
