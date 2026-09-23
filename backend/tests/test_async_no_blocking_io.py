# Tests - 静态守卫：async 路由内不得直接做同步 IO
"""覆盖任务书 8.4 提出的第 4 条建议，但把范围从「LLM 调用点」扩到「所有同步 IO」。

## 为什么需要这条守卫

这一轮修的是「LLM 调用阻塞事件循环」，但同类问题在项目里远不止 LLM 一处：
Qdrant 的 `kb.sample()`、PostgreSQL 的 `list_sessions()`、Redis 的
`mm.get_session_context()`、甚至 `get_state_machine()` 的惰性 import（8s），
全都是「async 路由里做同步 IO」。

只按「哪些地方调了 LLM」这个维度去搜，必然漏掉同步的 DB / 向量库 / 缓存 / import。
正确的搜索维度是「**`async def` 函数体内出现了哪些同步调用**」——
本文件把这个维度固化成可回归的约束，而不是靠人记得。

## 为什么用 AST 而不是 grep

见 `tests/_async_io_guard.py` 的模块 docstring。核心是两类必然的错误：
`await` 的异步调用会被误判（`await kb.upsert(...)`），
换名的变量会被漏判（`kb = get_knowledge_base()` 之后 `kb.count()`）。

## 豁免方式

确实有意为之的同步 IO，在该行加注释 `# async-io-guard: allow`。
豁免是行内的、可见的、必须写理由 —— 不允许全局关掉守卫。
"""

from pathlib import Path

import pytest

import _async_io_guard as guard

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ROUTES_DIR = BACKEND_ROOT / "src" / "api" / "routes"


def _route_files() -> list[Path]:
    files = sorted(ROUTES_DIR.glob("*.py"))
    assert files, f"no route modules found under {ROUTES_DIR}"
    return files


# ==================== 主断言 ====================


def test_no_sync_io_in_async_routes():
    """`src/api/routes/*.py` 的每个 async 函数体内，都不得直接调用同步 IO。

    修复方式：`await asyncio.to_thread(fn, ...)`。多个互不依赖的调用用
    `asyncio.gather(...)` 包起来，还能顺带省掉串行等待的 RTT。
    """
    violations = guard.analyze_paths(_route_files())

    if violations:
        detail = "\n".join(f"  {v.render()}" for v in violations)
        pytest.fail(
            f"发现 {len(violations)} 处「async 函数体内直接调用同步 IO」：\n{detail}\n\n"
            "修法：await asyncio.to_thread(fn, ...)；互不依赖的多个调用用 asyncio.gather 并发。\n"
            "若确实有意为之，在该行加注释 `# async-io-guard: allow` 并写明理由。"
        )


# ==================== 守卫自检 ====================
# 一个「永远返回空列表」的守卫也能让上面那条断言通过。以下用例证明分析器
# 真的能识别违规、且真的会放过合法写法 —— 守卫有牙齿才值得信任。


def test_guard_detects_direct_sync_io():
    src = """
async def route():
    kb = get_knowledge_base()
    total = kb.count("repo")
"""
    found = guard.analyze_source(src, "synthetic.py")
    callees = {v.callee for v in found}
    assert "get_knowledge_base()" in callees
    # kb 由 IO 工厂赋值 ⇒ 视为「持有 IO 对象」，其方法调用也要报
    assert "kb.count()" in callees


def test_guard_detects_io_bearing_local_sync_helper():
    """本地同步函数做 IO、被 async 路由直接调用 —— 最容易被人工审查漏掉的一类。"""
    src = """
def _helper(item_id):
    return find_card(item_id)

async def route(item_id):
    card = _helper(item_id)
"""
    found = guard.analyze_source(src, "synthetic.py")
    assert any(v.callee == "_helper()" for v in found)


def test_guard_ignores_awaited_async_calls():
    """`await` 说明被调方是协程，不阻塞事件循环。漏掉这条会把 upsert 全误报。"""
    src = """
async def route(item):
    kb = get_knowledge_base()
    await kb.upsert(item)
"""
    found = guard.analyze_source(src, "synthetic.py")
    assert not any(v.callee == "kb.upsert()" for v in found)


def test_guard_ignores_to_thread_wrapped_calls():
    src = """
async def route(item_id):
    return await asyncio.to_thread(find_card, item_id)
"""
    assert guard.analyze_source(src, "synthetic.py") == []


def test_guard_ignores_calls_inside_gather():
    """gather 的实参是协程，不是同步调用。"""
    src = """
async def route(uid):
    a, b = await asyncio.gather(
        asyncio.to_thread(count_activities, uid),
        asyncio.to_thread(count_bookmarks, uid),
    )
"""
    assert guard.analyze_source(src, "synthetic.py") == []


def test_guard_ignores_data_access_on_io_results():
    """`find_cards()` 做 IO，但返回的是**数据**；`cards.get(id)` 只是查字典。

    这条是「误报」防线：不区分资源与数据的话，`session_ctx.get()` /
    `counts.get()` / `context.get()` 会全被误报，守卫随即被当成噪声关掉。
    """
    src = """
async def route(ids):
    cards = await asyncio.to_thread(find_cards, ids)
    return cards.get("x")
"""
    assert guard.analyze_source(src, "synthetic.py") == []


def test_guard_does_not_flag_random_sample():
    """`random.sample` 是纯计算，不能因为 `kb.sample()` 是 IO 就把它一起收进黑名单。"""
    src = """
async def route(pool):
    return random.sample(pool, 3)
"""
    assert guard.analyze_source(src, "synthetic.py") == []


def test_guard_honours_inline_allow_marker():
    src = """
async def route():
    total = kb.count("repo")  # async-io-guard: allow 有意为之
"""
    assert guard.analyze_source(src, "synthetic.py") == []


def test_guard_ignores_comments_and_strings():
    """正则方案会把注释里的 `kb.sample(` 当成违规，AST 不会。"""
    src = """
async def route():
    # 这里曾经直接调用 kb.sample(3)，现已卸载
    note = "kb.count('repo')"
    return note
"""
    assert guard.analyze_source(src, "synthetic.py") == []


# ==================== 守卫覆盖范围自检 ====================


def test_guard_covers_every_sync_service_used_by_routes():
    """黑名单必须覆盖路由实际用到的同步服务函数。

    这条防的是「黑名单漏项」——`count_activities` / `list_messages` 都曾被我漏掉，
    而漏项会让守卫在真实违规上沉默。做法：把路由里调用的所有裸函数名反查定义，
    凡是解析到「只有同步实现」且名字以 IO 语义命名的，都必须在黑名单里。
    """
    import ast

    # 明确已知的「同步但纯计算」函数：不阻塞事件循环，允许不在黑名单里
    PURE_SYNC_ALLOWED = {
        "UserPublic.from_user",
        "clamp_timeout",
        "is_database_configured",
        "log_downstream_failure",
        "rate_limit",
        "_attachment_disposition",
        "_ensure_db_available",
        "_format_uptime",
        "_generate_fallback_response",
        "_revive_context",
        "_redis_persistable",
        "_find_news_item",
        "_load_session_context",
        "_cache_session_context",
        "_personalized_items",
        "_get_state_machine",
        "_get_memory_manager",
    }

    # 路由模块里被调用的裸函数名 / 属性名
    called: set[str] = set()
    for path in _route_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        func = sub.func
                        if isinstance(func, ast.Name):
                            called.add(func.id)
                        elif isinstance(func, ast.Attribute) and isinstance(
                            func.value, ast.Name
                        ):
                            called.add(f"{func.value.id}.{func.attr}")

    # 已知的同步 IO 服务函数全集（与 _async_io_guard.KNOWN_IO_NAMES 对齐）
    must_be_covered = {
        "get_knowledge_base",
        "get_vectors_by_ids",
        "candidate_points",
        "find_card",
        "find_cards",
        "get_provenance",
        "build_user_profile",
        "log_activity",
        "list_activities",
        "count_activities",
        "add_bookmark",
        "remove_bookmark",
        "list_bookmarks",
        "count_bookmarks",
        "get_bookmarked_ids",
        "upsert_session",
        "load_session",
        "list_sessions",
        "get_session",
        "list_messages",
        "count_messages",
        "count_messages_by_session",
        "append_messages",
        "session_stats",
        "complete_session_record",
        "sync_state_from_context",
        "get_feed_cache",
        "save_feed_cache",
        "get_state_machine",
        "get_memory_manager",
    }

    missing = {
        name
        for name in must_be_covered
        if name in called and name not in guard.KNOWN_IO_NAMES
    }
    assert not missing, f"路由调用了这些同步 IO，但守卫黑名单漏了它们：{sorted(missing)}"

    # 反向检查：黑名单里的名字不该出现在「纯计算豁免」里，否则自相矛盾
    contradictory = guard.KNOWN_IO_NAMES & PURE_SYNC_ALLOWED
    assert not contradictory, f"既在黑名单又在纯计算豁免里：{sorted(contradictory)}"


def test_guard_actually_covered_something():
    """守卫必须扫到真实的 async 路由，避免路径写错导致「0 文件 = 0 违规」。"""
    files = _route_files()
    assert len(files) >= 4, f"只扫到 {len(files)} 个路由模块，路径可能不对：{files}"

    import ast

    async_fns = 0
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        async_fns += sum(1 for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))
    assert async_fns >= 10, f"只扫到 {async_fns} 个 async 函数，疑似解析失败"
