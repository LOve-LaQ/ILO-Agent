# Test Qdrant Timeout - Qdrant 按次超时必须是整数秒
"""锁住两件事，都是 2026-09-23 真实踩出来的坑。

## 坑一：按次传 float 超时会让 Qdrant 调用**必然失败**
qdrant-client 1.19 的 REST 客户端在 `qdrant_client/http/api_client.py:96` 做的是
`kwargs["timeout"] = int(kwargs["params"]["timeout"])`，而 `params["timeout"]` 在进入
这一行之前已经被序列化成字符串。于是 `6.0` 的路径是 `str(6.0)` → `"6.0"` →
`int("6.0")` → `ValueError: invalid literal for int() with base 10: '6.0'`。

异常发生在客户端构造请求时，**请求根本发不出去**。实测表现：一次手动抓取 23 条新卡
全部「入库失败」，批次落 `failed`，知识库一条没多 —— 而路由把它报成了
「✅ 新增 23 条」，用户看到的是「抓取成功但总数没变」。

这里用三层钉住：
- `qdrant_timeout_seconds` 的行为（返回 int / 向上取整 / 下限 1）
- AST 静态守卫：任何 Qdrant 按次 `timeout=` 都必须过这个助手
- 真实 Qdrant 集成测试（本机有 Qdrant 时才跑），并反向钉住第三方行为

## 坑二：采集失败被上报成成功
`collect_items` 曾把返回给调用方的 `status` 硬编码为 `"ok"`，与它自己刚写进批次表的
`failed` 终态自相矛盾。前端据此弹出绿色成功提示，失败被 UI 抹平。
"""

import ast
import asyncio
import pathlib

import pytest

from src.core.config import settings
from src.core.resilience import qdrant_timeout_seconds
from src.services import collection_service as cs

SRC_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src"

# Qdrant 客户端上「接受按次 timeout」的方法。只有这些方法会踩到 int(str(x)) 那个坑，
# 构造期 `QdrantClient(url=..., timeout=4.0)` 走 httpx 自己的超时，传 float 是安全的
# （因此**不在**此列，且守卫只认属性调用，不会误伤）。
QDRANT_METHODS = frozenset(
    {
        "upsert",
        "query_points",
        "scroll",
        "count",
        "retrieve",
        "search",
        "search_batch",
        "recommend",
        "recommend_batch",
        "set_payload",
        "overwrite_payload",
        "delete_payload",
        "clear_payload",
        "delete",
        "delete_vectors",
        "update_vectors",
        "create_collection",
        "delete_collection",
        "get_collection",
        "get_collections",
        "update_collection",
    }
)


# ---------------------------------------------------------------- 助手行为

def test_helper_returns_int():
    """契约就是「返回 int」—— 传 float 给 qdrant-client 会炸，这是唯一的修复方式"""
    for seconds in (6.0, 4.0, 2.5, 0.3, 1.0, 90.0):
        assert isinstance(qdrant_timeout_seconds(seconds), int)


def test_helper_keeps_whole_seconds():
    assert qdrant_timeout_seconds(6.0) == 6
    assert qdrant_timeout_seconds(4.0) == 4
    assert qdrant_timeout_seconds(90.0) == 90


def test_helper_rounds_up_instead_of_truncating():
    """int(2.5) 会截成 2，让我们比配置的预算更早放弃 —— 必须向上取整"""
    assert qdrant_timeout_seconds(2.5) == 3
    assert qdrant_timeout_seconds(0.3) == 1


def test_helper_never_returns_zero():
    """在 Qdrant 的语义里 timeout=0 是「不设超时」，与「预算已耗尽」正好相反"""
    for seconds in (0, 0.0, -1.0):
        assert qdrant_timeout_seconds(seconds) == 1


def test_helper_accepts_the_configured_budgets():
    """配置里的读/写档必须都能安全转换（它们是 .env 可覆盖的 float）"""
    assert isinstance(qdrant_timeout_seconds(settings.qdrant_timeout), int)
    assert isinstance(qdrant_timeout_seconds(settings.qdrant_write_timeout), int)


# ---------------------------------------------------------------- AST 静态守卫

def _violations(source: str, filename: str) -> list:
    """找出「Qdrant 按次 timeout 没过 qdrant_timeout_seconds」的调用点"""
    tree = ast.parse(source, filename=filename)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # 只认属性调用（`kb.upsert(...)`）。构造期 `QdrantClient(...)` 是名字调用，
        # 天然被排除 —— 那是刻意允许传 float 的唯一位置。
        if not isinstance(func, ast.Attribute) or func.attr not in QDRANT_METHODS:
            continue
        for keyword in node.keywords:
            if keyword.arg != "timeout":
                continue
            value = keyword.value
            wrapped = (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "qdrant_timeout_seconds"
            )
            if not wrapped:
                found.append(
                    f"{filename}:{node.lineno} {func.attr}(timeout=...) "
                    "未过 qdrant_timeout_seconds"
                )
    return found


def test_no_raw_per_call_timeout_on_qdrant_calls():
    """主断言：src/ 下不允许出现裸的 Qdrant 按次 timeout"""
    files = sorted(SRC_ROOT.rglob("*.py"))
    assert files, "没扫到任何源文件，守卫等于没生效"

    violations = []
    for path in files:
        violations += _violations(
            path.read_text(encoding="utf-8"), str(path.relative_to(SRC_ROOT))
        )

    assert not violations, (
        "Qdrant 按次超时必须过 qdrant_timeout_seconds 转成整数秒"
        "（float 会让 int(str(x)) 抛 ValueError，请求根本发不出去）：\n"
        + "\n".join(violations)
    )


def test_guard_detects_raw_float_timeout():
    """守卫自检：它必须真的能抓到坏代码，否则主断言是空的"""
    source = "client.upsert(collection_name='c', points=[], timeout=6.0)\n"
    assert _violations(source, "bad.py")


def test_guard_detects_raw_int_timeout():
    """裸 int 也要拦：超时预算必须有单一出处，散落的常量会让 .env 覆盖失效"""
    source = "client.query_points(query=[], limit=1, timeout=4)\n"
    assert _violations(source, "bad.py")


def test_guard_allows_wrapped_timeout():
    source = (
        "client.upsert(points=[], "
        "timeout=qdrant_timeout_seconds(settings.qdrant_write_timeout))\n"
    )
    assert not _violations(source, "good.py")


def test_guard_ignores_other_downstream_timeouts():
    """误报比漏报致命 —— 它会让守卫被当成噪声然后被关掉。
    httpx / 构造函数都不是 Qdrant 的按次超时，不得误报。"""
    source = (
        "httpx.get(url, timeout=5.0)\n"
        "client.post(path, timeout=10.0)\n"
        "QdrantClient(url=url, timeout=4.0)\n"
    )
    assert not _violations(source, "ok.py")


def test_guard_covers_the_real_call_sites():
    """交叉检查：守卫覆盖的方法名集合不能漏掉真实存在的调用点"""
    tech = (SRC_ROOT / "modules/discovery/tech_knowledge.py").read_text(encoding="utf-8")
    memory = (SRC_ROOT / "modules/agent/memory_manager.py").read_text(encoding="utf-8")

    assert "qdrant_timeout_seconds(settings.qdrant_write_timeout)" in tech
    assert memory.count("qdrant_timeout_seconds(settings.qdrant_timeout)") == 2
    # 构造期必须保持原样（包上也无害，但会让人误以为构造期也踩坑）
    assert "QdrantClient(url=settings.qdrant_url, timeout=settings.qdrant_timeout)" in tech


# ---------------------------------------------------------------- 真实 Qdrant 集成

def _qdrant_reachable() -> bool:
    try:
        import httpx

        resp = httpx.get(f"{settings.qdrant_url.rstrip('/')}/healthz", timeout=1.0)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001 - 探测失败即视为不可达
        return False


requires_qdrant = pytest.mark.skipif(
    not _qdrant_reachable(),
    reason="Qdrant 未运行（本地开发才有；CI 上跳过）",
)

_PROBE_COLLECTION = "pytest_qdrant_timeout_tmp"


def _drop_probe(client) -> None:
    try:
        client.delete_collection(_PROBE_COLLECTION)
    except Exception:  # noqa: BLE001 - 不存在就算了
        pass


@requires_qdrant
def test_helper_output_is_accepted_by_real_qdrant():
    """端到端：助手产出的整数秒必须能真正完成一次 upsert + query_points"""
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    client = QdrantClient(url=settings.qdrant_url)
    _drop_probe(client)
    client.create_collection(
        _PROBE_COLLECTION,
        vectors_config=VectorParams(size=8, distance=Distance.COSINE),
    )
    try:
        client.upsert(
            _PROBE_COLLECTION,
            points=[PointStruct(id=1, vector=[0.1] * 8, payload={"type": "repo"})],
            timeout=qdrant_timeout_seconds(settings.qdrant_write_timeout),
        )
        got = client.query_points(
            _PROBE_COLLECTION,
            query=[0.1] * 8,
            limit=1,
            timeout=qdrant_timeout_seconds(settings.qdrant_timeout),
        )
        assert len(got.points) == 1
    finally:
        _drop_probe(client)


@requires_qdrant
def test_raw_float_timeout_really_breaks_qdrant_client():
    """反向钉住第三方行为。

    这个测试断言的是 **qdrant-client 的 bug**，不是我们的契约。它存在的意义是：
    哪一天 qdrant-client 修掉了 `int(str(x))`，这里会失败并提醒我们
    「qdrant_timeout_seconds 这层包装可以删了」，而不是让它悄悄变成无用代码。
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    client = QdrantClient(url=settings.qdrant_url)
    _drop_probe(client)
    client.create_collection(
        _PROBE_COLLECTION,
        vectors_config=VectorParams(size=8, distance=Distance.COSINE),
    )
    try:
        with pytest.raises(ValueError, match="invalid literal for int"):
            client.upsert(
                _PROBE_COLLECTION,
                points=[PointStruct(id=1, vector=[0.1] * 8, payload={})],
                timeout=settings.qdrant_write_timeout,  # float，故意不转换
            )
    finally:
        _drop_probe(client)


# ---------------------------------------------------------------- 采集状态如实上报

def _patch_side_effects(monkeypatch) -> dict:
    """把批次表 / 去重 / Redis 标记全部换成内存桩，让编排逻辑可离线测"""
    captured: dict = {}
    monkeypatch.setattr(cs, "start_batch", lambda *a, **kw: None)
    monkeypatch.setattr(cs, "finish_batch", lambda batch_id, **kw: captured.update(kw))
    monkeypatch.setattr(cs, "get_collected_ids", lambda ids: set())
    return captured


def _collect(monkeypatch, store, count: int = 3):
    """跑一次 collect_items，返回 (CollectResult, 落批次时的 kwargs)"""
    captured = _patch_side_effects(monkeypatch)
    items = [{"id": f"gh-{i}", "title": f"t{i}"} for i in range(count)]

    async def fetch():
        return items

    result = asyncio.run(
        cs.collect_items(
            kind="repo",
            fetch=fetch,
            store=store,
            summarize=False,  # 不走 LLM，卡片按原样即终态
            persist=False,  # 不落溯源记录、不写 Redis 标记
        )
    )
    return result, captured


def test_all_store_failures_report_error(monkeypatch):
    """本次事故的核心：23 条全部入库失败，绝不能报成 ok"""

    async def boom(_item):
        raise ValueError("invalid literal for int() with base 10: '6.0'")

    result, captured = _collect(monkeypatch, boom)

    assert result.status == "error", "全部失败必须报 error，否则前端会弹绿色成功提示"
    assert result.new_count == 3
    assert result.failed_count == 3
    assert captured["status"] == "failed"
    assert captured["error"], "失败批次必须留下原因，否则排查只能翻日志"


def test_partial_store_failures_report_partial(monkeypatch):
    calls = {"n": 0}

    async def sometimes(_item):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")

    result, captured = _collect(monkeypatch, sometimes)

    assert result.status == "partial"
    assert result.failed_count == 1
    assert captured["status"] == "partial"
    assert captured["error"]


def test_successful_store_reports_ok(monkeypatch):
    async def fine(_item):
        return None

    result, captured = _collect(monkeypatch, fine)

    assert result.status == "ok"
    assert result.failed_count == 0
    assert captured["status"] == "succeeded"
    assert captured["error"] is None, "成功批次不该写 error"


def test_empty_fetch_still_reports_empty(monkeypatch):
    """empty 是「没抓到数据」的独立语义，不能被 status 改造顺手改掉"""
    captured = _patch_side_effects(monkeypatch)

    async def fetch():
        return []

    result = asyncio.run(
        cs.collect_items(
            kind="repo", fetch=fetch, store=None, summarize=False, persist=False
        )
    )

    assert result.status == "empty"
    assert captured["status"] == "succeeded"


def test_refresh_response_exposes_failed_count():
    """前端要如实提示失败条数，这个字段是契约的一部分"""
    from src.schemas.discover import RefreshResponse

    fields = RefreshResponse.model_fields
    assert "failed_count" in fields, "缺少 failed_count，前端无法知道有多少条没入库"
    description = fields["status"].description or ""
    for token in ("ok", "partial", "error", "empty"):
        assert token in description, f"status 的取值说明缺少 {token}"
