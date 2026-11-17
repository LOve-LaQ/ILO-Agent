# Tests - 采集任务异步化（202 受理 / 幂等锁 / 进度状态机）
"""覆盖本次改造的四条不变量，每条都对应一类真实的线上事故：

1. **POST /refresh 立即返回 202**：改造前它同步跑完整条抓取链路（1~3 分钟），
   浏览器与反向代理先超时断开，用户重试一次就付两遍 LLM 钱；
2. **同一主体并发触发只放行一个**：异步化之后重复触发的后果从「浪费一次请求」
   升级成「后台并行跑两条链路」，必须有幂等锁，且 409 要带上正在跑的 task_id
   让前端能接着看进度；
3. **进度语义正确**：`total` 在不同阶段含义不同（抓到 50 条 / 待摘要 23 条），
   跨阶段沿用会让进度条显示「23/50 = 100%」这种一眼假的数字 —— 进度条说谎
   比没有更糟；
4. **异常必须落 failed**：停在 running 的任务会让前端永远转圈，且批次表里
   留下一条假的「进行中」，把「最近一次采集成没成」这个问题彻底搞乱。

全部离线：后台执行体被替换成假协程，不联网、不碰 Qdrant、不写库。
"""

import asyncio
import time
import uuid

import pytest

from src.services import task_runner
from src.services.task_runner import TaskState


@pytest.fixture(autouse=True)
def _clean_tasks():
    """任务表、锁、Redis 三者都是跨用例状态，必须逐个用例隔离。

    **锁必须注入 fakeredis 而不是用本机 Redis**：一次用例留下的锁 TTL 是 30
    分钟，会静默地把后面所有用例挡在 409 上（本套第一次跑就踩了这个坑 ——
    「A 用例通过、B 用例莫名 409」，排查成本极高）。
    """
    import fakeredis

    from src.core import redis_client

    redis_client.set_redis_client(fakeredis.FakeRedis(decode_responses=True))
    task_runner.reset_tasks()
    yield
    task_runner.reset_tasks()
    redis_client.reset_redis_client()


@pytest.fixture
def stub_refresh(monkeypatch):
    """把仓库抓取的后台执行体换成假协程（只验证任务编排，不触网）"""
    from src.api.routes import discover as discover_module

    calls = []

    async def _fake(state, *, limit, force, operator_user_id=None):
        calls.append({"task_id": state.task_id, "limit": limit})
        state.advance("fetching", 0, 0)
        state.advance("summarizing", 2, 2)
        state.status = "succeeded"
        state.new_count = 2

    monkeypatch.setattr(discover_module, "_execute_refresh_repos", _fake)
    return calls


@pytest.fixture
def slow_refresh(monkeypatch):
    """慢任务：用来构造「第一个还没跑完就再次提交」的并发场景"""
    from src.api.routes import discover as discover_module

    async def _slow(state, **kwargs):
        await asyncio.sleep(0.5)
        state.status = "succeeded"

    monkeypatch.setattr(discover_module, "_execute_refresh_repos", _slow)
    monkeypatch.setattr(discover_module, "_execute_refresh_articles", _slow)


def _poll(client, task_id: str, *, attempts: int = 60, interval: float = 0.05):
    """轮询到终态（TestClient 的事件循环跑在独立线程，主线程 sleep 期间它继续推进）"""
    snapshot = None
    for _ in range(attempts):
        resp = client.get(f"/api/v1/discover/tasks/{task_id}")
        assert resp.status_code == 200, resp.text
        snapshot = resp.json()
        if snapshot["status"] != "running":
            return snapshot
        time.sleep(interval)
    return snapshot


# ---------------------------------------------------------------- 受理与进度
def test_refresh_returns_202_without_waiting(client, stub_refresh):
    """接口必须立即返回，而不是等抓取跑完"""
    resp = client.post("/api/v1/discover/refresh?limit=5")

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["task_id"]
    assert body["status"] == "running"
    assert body["kind"] == "repo"
    # poll_url 由后端给出，前端不必自己拼路径（拼错是很容易发生的低级故障）
    assert body["poll_url"].endswith(body["task_id"])


def test_task_progress_reaches_terminal(client, stub_refresh):
    task_id = client.post("/api/v1/discover/refresh?limit=5").json()["task_id"]

    snapshot = _poll(client, task_id)

    assert snapshot["status"] == "succeeded"
    assert snapshot["stage"] == "done"
    assert snapshot["new_count"] == 2
    assert snapshot["percent"] == 100
    assert snapshot["finished_at"] is not None
    # 假执行体确实被调用了（否则上面的断言可能只是「任务没开始就成功」的假象）
    assert stub_refresh and stub_refresh[0]["limit"] == 5


def test_duplicate_trigger_returns_409_with_running_task_id(client, slow_refresh):
    first = client.post("/api/v1/discover/refresh?limit=5")
    assert first.status_code == 202
    task_id = first.json()["task_id"]

    second = client.post("/api/v1/discover/refresh?limit=5")

    assert second.status_code == 409
    body = second.json()
    assert body["code"] == "TASK_ALREADY_RUNNING"
    # 关键：要把正在跑的那个 id 告诉前端，前端才能接着看进度而不是报错了事
    assert body["detail"]["task_id"] == task_id


def test_article_and_repo_share_the_same_lock(client, slow_refresh):
    """两条抓取通道打的是同一个知识库与同一批 LLM 配额，必须共用一把锁"""
    assert client.post("/api/v1/discover/refresh?limit=5").status_code == 202

    resp = client.post("/api/v1/discover/refresh-articles?per_platform=2")

    assert resp.status_code == 409
    assert resp.json()["code"] == "TASK_ALREADY_RUNNING"


def test_unknown_task_returns_404(client):
    resp = client.get(f"/api/v1/discover/tasks/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------- 状态机语义
def test_advance_resets_counts_when_stage_changes():
    """抓取 50 条、去重后只剩 23 条：total 必须跟着阶段变，不能沿用 50"""
    state = TaskState(task_id="t", kind="repo")

    state.advance("deduping", 0, 50)
    assert (state.processed, state.total) == (0, 50)

    state.advance("summarizing", 0, 23)
    assert (state.processed, state.total) == (0, 23), "阶段切换后计数必须清零重来"

    state.advance("summarizing", 23, 23)
    assert state.snapshot()["percent"] == 100


def test_snapshot_never_invents_progress_when_total_unknown():
    state = TaskState(task_id="t", kind="repo")
    state.advance("fetching", 0, 0)

    assert state.snapshot()["percent"] == 0

    state.advance("fetching", 7, 0)
    # 总数未知时不推算百分比：编出来的进度比没有进度更误导
    assert state.snapshot()["percent"] == 0


def test_task_exception_lands_failed_not_running(monkeypatch):
    """停在 running 的任务会让前端永远转圈 —— 异常必须落终态"""
    # 不碰真实 Redis：锁走进程内降级路径（本用例只关心状态落定）
    monkeypatch.setattr(task_runner, "get_redis", lambda: None)

    async def _boom(state):
        raise RuntimeError("抓取炸了")

    async def _run():
        assert task_runner.acquire("test", "sub", task_id="t1") is None
        state = TaskState(task_id="t1", kind="repo")
        task = task_runner.spawn(state, _boom, scope="test", subject="sub")
        await task
        return state

    state = asyncio.run(_run())

    assert state.status == "failed"
    assert state.stage == "failed"
    assert "抓取炸了" in (state.error or "")
    assert state.finished_at is not None, "终态任务必须有结束时间，否则无法算耗时"
    # 锁必须释放：否则这个主体在 TTL 内再也提交不了任务
    assert task_runner.acquire("test", "sub", task_id="t2") is None


# ---------------------------------------------------------------- 幂等锁
def test_lock_is_exclusive_and_released():
    assert task_runner.acquire("s", "sub", task_id="a") is None
    assert task_runner.acquire("s", "sub", task_id="b") == "a"

    task_runner.release("s", "sub", task_id="a")

    assert task_runner.acquire("s", "sub", task_id="b") is None


def test_lock_only_releases_own_holder():
    """释放别人的锁会把正在跑的任务暴露给第三个请求"""
    assert task_runner.acquire("s", "sub", task_id="a") is None

    task_runner.release("s", "sub", task_id="someone-else")

    assert task_runner.acquire("s", "sub", task_id="b") == "a"


def test_lock_falls_back_to_local_when_redis_unavailable(monkeypatch):
    """Redis 不可用时**不能**退化成放行：重复触发等于重复付费

    这一点与限流的 fail-open 是**相反**的策略，因为有意的：少一层防护可以接受，
    多付一次 LLM 费用不可以。
    """
    monkeypatch.setattr(task_runner, "get_redis", lambda: None)

    assert task_runner.acquire("s2", "sub", task_id="a") is None
    assert task_runner.acquire("s2", "sub", task_id="b") == "a"


# ---------------------------------------------------------------- 结果映射
def test_apply_result_maps_error_to_failed_with_reason():
    """`status=error` 表示「新卡全部没进库」，任务本身是跑完了的，但必须报失败"""
    from src.api.routes.discover import _apply_result
    from src.services.collection_service import CollectResult

    state = TaskState(task_id="t", kind="repo")
    _apply_result(
        state,
        CollectResult(status="error", kind="repo", new_count=5, failed_count=5),
    )

    assert state.status == "failed"
    # 只写 status 不写 error，运维就只能翻日志才知道为什么全失败
    assert "5 条" in (state.error or "")


def test_apply_result_keeps_partial_counts():
    from src.api.routes.discover import _apply_result
    from src.services.collection_service import CollectResult

    state = TaskState(task_id="t", kind="repo")
    _apply_result(
        state,
        CollectResult(
            status="partial", kind="repo", new_count=3, skipped_count=1, failed_count=2
        ),
    )

    assert state.status == "partial"
    assert state.new_count == 3
    assert state.skipped_count == 1
    assert state.failed_count == 2
    assert state.message and "2" in state.message
