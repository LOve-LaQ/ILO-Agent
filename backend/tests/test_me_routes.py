# Tests - 个人中心、内容溯源与历史回填（真实读写 PostgreSQL）
"""阶段 3 验收测试：/me/*（收藏 / 学习记录 / 行为时间线 / 概览）、
/discover/cards/{id}/provenance 内容溯源，以及 collection_service.backfill_records
（把阶段 3 之前入库的老卡片补上溯源记录）。

走真实链路（真实注册用户 + 真实 PostgreSQL，不覆盖任何依赖）：统一用
`anonymous_client` 拿到「未注入假用户」的客户端，再自行带真实 Bearer token，
与 tests/test_auth.py 的做法一致 —— 假用户 id 在库里不存在，既写不进也查不出。

测试账号与造出来的卡片 id 统一带固定前缀，模块结束后连同衍生数据一起清理；
脚本中途失败留下的残留会在下一次运行时先被回收。
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import delete, select, text

from src.core.db import get_session_factory, is_database_configured
from src.models.engagement import UserActivity
from src.models.learning import ChatMessage, LearningSession
from src.models.user import User

pytestmark = pytest.mark.skipif(
    not is_database_configured(),
    reason="需要 DATABASE_URL（本机 PostgreSQL + backend/.env）",
)

EMAIL_PREFIX = "metest_"
PROV_ITEM_PREFIX = "metest-prov-"
PASSWORD = "Str0ng-Passw0rd"


def _payload() -> dict:
    token = uuid.uuid4().hex[:10]
    return {
        "email": f"{EMAIL_PREFIX}{token}@example.com",
        "username": f"me_{token}",
        "password": PASSWORD,
    }


def _register(client) -> dict:
    resp = client.post("/api/v1/auth/register", json=_payload())
    assert resp.status_code == 201, resp.text
    return resp.json()


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _purge() -> None:
    """清掉本模块造的账号及其衍生数据。

    只删 users 不够：`chat_messages.session_id` 没有外键（临时会话也要留痕），
    `user_activities.user_id` 是 ON DELETE SET NULL（删用户只会留下孤儿行）。
    """
    with get_session_factory()() as session:
        user_ids = (
            session.execute(select(User.id).where(User.email.like(f"{EMAIL_PREFIX}%")))
            .scalars()
            .all()
        )
        if user_ids:
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

        # 内容溯源：本模块自造的采集记录与批次
        session.execute(
            text("delete from collection_records where item_id like :p"),
            {"p": f"{PROV_ITEM_PREFIX}%"},
        )
        session.execute(
            text("delete from collection_batches where params->>'source' = :s"),
            {"s": EMAIL_PREFIX},
        )
        session.commit()


@pytest.fixture(scope="module", autouse=True)
def _cleanup_accounts():
    """开跑前先回收上轮残留（用例中途崩溃也能自愈），跑完再清理"""
    _purge()
    yield
    _purge()


@pytest.fixture(scope="module")
def accounts(raw_client):
    """注册两个真实用户：第一个是被测主体，第二个用于越权验证"""
    return _register(raw_client), _register(raw_client)


def _run_session(client, token: str, item_id: str = "metest-card") -> str:
    """走完一次学习会话（讲解 → 对话 → 测验 → 完成），返回 session_id。

    `item_id` 故意用知识库里不存在的 id：此时 core_concepts 为空，
    正好覆盖「降级测验题在缺概念时不能崩」这一回归点。
    """
    headers = _hdr(token)

    created = client.post(
        "/api/v1/learning/session",
        json={"news_item_id": item_id, "title": "个人中心测试主题"},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]

    explained = client.post(
        "/api/v1/learning/response",
        json={"session_id": session_id, "user_input": "开始学习"},
        headers=headers,
    )
    assert explained.status_code == 200, explained.text
    total = (explained.json().get("quiz") or {}).get("total") or 0
    assert total > 0, "讲解必须产出测验题，否则分数无从审计"

    chatted = client.post(
        "/api/v1/learning/chat",
        json={"session_id": session_id, "message": "再解释一下"},
        headers=headers,
    )
    assert chatted.status_code == 200, chatted.text

    quizzed = client.post(
        "/api/v1/learning/quiz",
        json={"session_id": session_id, "user_answers": [0] * total},
        headers=headers,
    )
    assert quizzed.status_code == 200, quizzed.text

    done = client.post(
        "/api/v1/learning/complete", json={"session_id": session_id}, headers=headers
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "completed"
    return session_id


# ==================== 概览 ====================


def test_profile_returns_own_account(anonymous_client, accounts):
    me, _ = accounts
    resp = anonymous_client.get("/api/v1/me/profile", headers=_hdr(me["access_token"]))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["id"] == me["user"]["id"]
    # 注册本身就是一条行为流水：行为溯源从账号诞生的那一刻就能对上账
    assert body["stats"]["activities_total"] >= 1
    assert body["stats"]["sessions_total"] >= 0


# ==================== 收藏 ====================


def test_bookmark_add_is_idempotent_and_carries_card(anonymous_client, accounts):
    me, _ = accounts
    headers = _hdr(me["access_token"])

    empty = anonymous_client.get("/api/v1/me/bookmarks", headers=headers).json()
    assert empty["source"] == "postgres", "数据库可用时不应报 unavailable"
    assert empty["total"] == 0

    first = anonymous_client.post("/api/v1/me/bookmarks/article-001", headers=headers)
    assert first.status_code == 200, first.text
    assert first.json() == {"item_id": "article-001", "bookmarked": True, "changed": True}

    again = anonymous_client.post("/api/v1/me/bookmarks/article-001", headers=headers)
    assert again.json()["changed"] is False, "重复收藏必须幂等"

    listed = anonymous_client.get("/api/v1/me/bookmarks", headers=headers).json()
    assert listed["count"] == 1
    assert listed["item_ids"] == ["article-001"]
    # 卡片快照随列表返回：前端不必再逐个查卡片
    assert listed["items"][0]["card"] is not None

    removed = anonymous_client.delete("/api/v1/me/bookmarks/article-001", headers=headers)
    assert removed.json() == {"item_id": "article-001", "bookmarked": False, "changed": True}
    assert (
        anonymous_client.delete(
            "/api/v1/me/bookmarks/article-001", headers=headers
        ).json()["changed"]
        is False
    )


def test_bookmarks_are_isolated_between_users(anonymous_client, accounts):
    me, other = accounts
    anonymous_client.post("/api/v1/me/bookmarks/article-001", headers=_hdr(me["access_token"]))

    listed = anonymous_client.get(
        "/api/v1/me/bookmarks", headers=_hdr(other["access_token"])
    ).json()
    assert listed["count"] == 0, "他人的收藏不可见"


# ==================== 学习记录 ====================


def test_session_history_survives_without_redis(anonymous_client, accounts):
    me, _ = accounts
    headers = _hdr(me["access_token"])
    session_id = _run_session(anonymous_client, me["access_token"])

    listed = anonymous_client.get("/api/v1/me/sessions", headers=headers).json()
    assert listed["total"] >= 1
    row = next((s for s in listed["items"] if s["session_id"] == session_id), None)
    assert row is not None, "刚结束的会话必须能从 PostgreSQL 回看到"
    assert row["state"] == "completed"
    assert row["message_count"] > 0, "对话条数应由一次 group by 统计出来"
    assert row["quiz_score"] is not None, "分数是学习效果的唯一凭证，必须落库"

    completed = anonymous_client.get(
        "/api/v1/me/sessions", params={"state": "completed"}, headers=headers
    ).json()
    assert completed["items"], "按状态过滤不应把结果过滤没了"
    assert all(s["state"] == "completed" for s in completed["items"])

    detail = anonymous_client.get(f"/api/v1/me/sessions/{session_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["session"]["message_count"] == len(body["messages"])
    assert body["messages"], "详情必须带完整对话历史"
    assert all(m["role"] in {"user", "assistant"} for m in body["messages"])
    assert all(m["created_at"] for m in body["messages"])

    # 回归：`chat_messages.created_at` 曾依赖列上的 `server_default=now()`，而 `now()` 取的是
    # **事务时间戳** —— 同一轮一次性插入的 user + assistant 会拿到完全相同的值。两个后果：
    # 按 created_at 升序回看时先后无从确定（可能颠倒），客户端拿时间戳当列表 key 还会撞
    # key 丢消息。落库时改为显式按毫秒递增，这里把「唯一且递增」钉住。
    stamps = [datetime.fromisoformat(m["created_at"]) for m in body["messages"]]
    assert len(set(stamps)) == len(stamps), "同一会话内每条消息的时间戳必须唯一"
    assert stamps == sorted(stamps), "详情必须按时间升序返回，回看的先后顺序才确定"


def test_session_detail_of_another_user_is_not_found(anonymous_client, accounts):
    me, other = accounts
    session_id = _run_session(anonymous_client, me["access_token"], item_id="metest-card-x")

    resp = anonymous_client.get(
        f"/api/v1/me/sessions/{session_id}", headers=_hdr(other["access_token"])
    )
    # 不存在与不属于自己都回 404：不泄露「这个 session_id 是否存在」
    assert resp.status_code == 404
    assert resp.json()["code"] == "SESSION_NOT_FOUND"


# ==================== 行为时间线 ====================


def test_activities_timeline_traces_every_action(anonymous_client, accounts):
    me, _ = accounts
    headers = _hdr(me["access_token"])
    _run_session(anonymous_client, me["access_token"], item_id="metest-card-act")
    anonymous_client.post(
        "/api/v1/learning/response",
        json={"session_id": "sess_ghost", "user_input": "开始学习"},
        headers=headers,
    )

    body = anonymous_client.get("/api/v1/me/activities", headers=headers).json()
    assert body["count"] > 0
    # request_id 与后端日志同一个 id，出问题能对上账
    assert all(item["request_id"] for item in body["items"])

    kinds = {item["action_type"] for item in body["items"]}
    for expected in (
        "register",
        "bookmark",
        "start_session",
        "process_response",
        "ask_question",
        "submit_quiz",
        "complete_session",
    ):
        assert expected in kinds, f"行为链缺 {expected}，实际：{sorted(kinds)}"

    filtered = anonymous_client.get(
        "/api/v1/me/activities", params={"action_type": "bookmark"}, headers=headers
    ).json()
    assert all(item["action_type"] == "bookmark" for item in filtered["items"])
    assert filtered["count"] <= filtered["total"]

    bad = anonymous_client.get(
        "/api/v1/me/activities", params={"action_type": "not_a_real_action"}, headers=headers
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "INVALID_ACTION_TYPE"


# ==================== 内容溯源 ====================


def test_provenance_reports_unknown_for_builtin_sample(anonymous_client):
    resp = anonymous_client.get("/api/v1/discover/cards/article-001/provenance")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["known"] is False, "内置示例没有采集记录"
    assert body["card"] is not None, "没有溯源也要能照常展示卡片"


def test_provenance_returns_404_for_unknown_card(anonymous_client):
    resp = anonymous_client.get("/api/v1/discover/cards/metest-prov-ghost/provenance")
    assert resp.status_code == 404
    assert resp.json()["code"] == "CARD_NOT_FOUND"


def test_provenance_links_card_to_batch_and_raw_text(anonymous_client, accounts):
    from src.services import collection_service as cs

    me, _ = accounts
    headers = _hdr(me["access_token"])
    item_id = f"{PROV_ITEM_PREFIX}full"

    batch_id = cs.start_batch("article", trigger="manual", params={"source": EMAIL_PREFIX})
    cs.record_item(
        batch_id,
        {
            "id": item_id,
            # 完整 TechCard 形状：snapshot 按 TechCard 校验
            "type": "repo",
            "title": "溯源验证仓库卡",
            "category": "backend",
            "source_platform": "github",
            "source_url": "https://example.com/metest",
            "raw_description": "采集时的原文",
            "summary": "摘要后的内容",
        },
        status="summarized",
    )
    cs.finish_batch(batch_id, status="succeeded", fetched_count=1, new_count=1)

    before = anonymous_client.get("/api/v1/me/activities", headers=headers).json()["count"]
    body = anonymous_client.get(f"/api/v1/discover/cards/{item_id}/provenance").json()
    after_anon = anonymous_client.get("/api/v1/me/activities", headers=headers).json()["count"]

    assert body["known"] is True
    assert body["source_platform"] == "github"
    assert body["source_url"].endswith("/metest")
    assert body["raw_description"] == "采集时的原文"
    assert body["batch"]["id"] == str(batch_id)
    assert body["batch"]["status"] == "succeeded"
    # 入库当时的快照 vs 当前卡片：force 覆盖写入后两者会不同
    assert body["snapshot"]["summary"] == "摘要后的内容"
    assert after_anon == before, "匿名浏览不落流水（也避免刷流量污染行为时间线）"

    anonymous_client.get(f"/api/v1/discover/cards/{item_id}/provenance", headers=headers)
    after_auth = anonymous_client.get("/api/v1/me/activities", headers=headers).json()["count"]
    assert after_auth == before + 1, "登录后访问应记录一条 view_card"


# ==================== 历史卡片回填 ====================


def test_backfill_marks_legacy_cards_and_never_overwrites():
    from src.services import collection_service as cs

    legacy_id = f"{PROV_ITEM_PREFIX}legacy"
    existing_id = f"{PROV_ITEM_PREFIX}existing"
    oversized_id = PROV_ITEM_PREFIX + "x" * 80

    # 先用真实采集路径写一条：回填必须跳过它，近似值不能覆盖真实溯源
    batch_id = cs.start_batch("repo", trigger="manual", params={"source": EMAIL_PREFIX})
    cs.record_item(
        batch_id,
        {
            "id": existing_id,
            "type": "repo",
            "title": "真实采集卡",
            "category": "backend",
            "source_platform": "github",
            "source_url": "https://example.com/real",
            "raw_description": "真实原文",
            "summary": "真实摘要",
        },
        status="summarized",
    )

    stats = cs.backfill_records(
        [
            # 阶段 3 之前的老卡片：只有展示名 source + link，没有溯源字段
            {
                "id": legacy_id,
                "type": "repo",
                "title": "老卡片",
                "category": "tools",
                "source": "GitHub Trending",
                "link": "https://github.com/legacy/card",
                "summary": "老卡片摘要",
                "published_at": "2024-03-05T10:00:00Z",
            },
            # 已有记录：不许被覆盖
            {"id": existing_id, "title": "不该生效", "summary": "不该生效"},
            # item_id 超出 collection_records.item_id 的列宽，如实报告而不是静默丢弃
            {"id": oversized_id, "title": "过长 id"},
            # 没有 id 的条目直接忽略
            {"title": "没有 id"},
        ]
    )

    assert stats == {"scanned": 2, "backfilled": 1, "skipped": 1, "oversized": 1}, stats

    legacy = cs.get_provenance(legacy_id)
    assert legacy["backfilled"] is True, "回填必须自报家门"
    assert legacy["batch"] is None, "没有真实批次就不要编造批次"
    assert legacy["source_platform"] == "github", "展示名应映射为平台标识"
    assert legacy["source_url"] == "https://github.com/legacy/card"
    assert legacy["raw_description"] == "老卡片摘要", "缺原文时退用摘要"
    assert legacy["summary_generated_at"] is None, "不知道就不假装知道"
    # collected_at 取内容发布时间（近似），而不是「回填那一刻」
    assert legacy["collected_at"].year == 2024, legacy["collected_at"]

    kept = cs.get_provenance(existing_id)
    assert kept["backfilled"] is False
    assert kept["source_url"] == "https://example.com/real"
    assert kept["raw_description"] == "真实原文"
    assert kept["summary_generated_at"] is not None

    # 幂等：再跑一次只会计入 skipped
    again = cs.backfill_records([{"id": legacy_id, "title": "x"}])
    assert again == {"scanned": 1, "backfilled": 0, "skipped": 1, "oversized": 0}, again
