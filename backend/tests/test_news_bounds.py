"""/discover/news 的分页边界。

实测记录：修复前 limit=0 返回空列表却带 has_more=true，limit=-1 走 Python
负切片返回 n-1 条，limit=1000 一次吐 1000 条 —— 一个匿名可读的端点不该有这种敞口。
"""

import pytest


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=-1",
        "limit=101",
        "limit=1000",
        "offset=-1",
        "offset=-5",
    ],
)
def test_news_rejects_out_of_range_pagination(client, query):
    response = client.get(f"/api/v1/discover/news?{query}")
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_news_accepts_upper_bound(client):
    response = client.get("/api/v1/discover/news?limit=100&offset=0")
    assert response.status_code == 200
    assert len(response.json()["items"]) <= 100


def test_news_default_still_works(client):
    response = client.get("/api/v1/discover/news")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(body["items"])
