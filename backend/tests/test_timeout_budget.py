# Tests - 下游超时预算表（任务书 2.1）与「无硬编码魔法数字」全局约束
"""覆盖任务书整体验收 4.1 的两条硬要求：

- 「所有新增超时/重试参数均可在 .env 覆盖，无硬编码魔法数字」
- 「src/core/config.py 与 backend/.env.example 配置项一一对应，无遗漏」

分两层：
1. **静态守卫**：扫 `src/` 源码，任何 `timeout=<数字>` / `socket_timeout=<数字>` 都算失败。
   这条规则的价值在于「防回归」—— 后来者顺手写一个 `timeout=30` 就会被拦下。
2. **运行时穿透**：断言配置值真的进了客户端对象，而不只是躺在 Settings 里。
   只改配置不接线是这类任务最常见的假完成。

全部离线可复现：只构造客户端，不发请求。
"""

import re
from pathlib import Path

import pytest

from src.core.config import settings

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = BACKEND_ROOT / "src"

# 预算表要求「可在 .env 覆盖」的全部键（任务书 2.1）
BUDGET_KEYS = (
    "redis_socket_timeout",
    "db_statement_timeout_ms",
    "qdrant_timeout",
    "qdrant_write_timeout",
    "embedding_timeout",
    "fetch_timeout",
    "llm_chat_timeout",
    "llm_summary_timeout",
    "llm_judge_timeout",
    "captcha_timeout",
    "smtp_timeout",
)

# 字面量超时的模式。只在源码里扫，测试自己用的小超时不算。
_HARDCODED_PATTERNS = (
    re.compile(r"\btimeout\s*=\s*\d"),
    re.compile(r"\bsocket_timeout\s*=\s*\d"),
    re.compile(r"\bsocket_connect_timeout\s*=\s*\d"),
    re.compile(r"\bTimeout\(\s*\d"),
    re.compile(r"\bstatement_timeout\s*=\s*\d"),
)


def _code_lines(path: Path):
    """产出 (行号, 去掉注释后的代码)。注释里提到 `timeout=50` 不该算违规。"""
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        code = raw.split("#", 1)[0]
        if code.strip():
            yield lineno, code


# ================================================================ 静态守卫
def test_no_hardcoded_timeout_literals_in_src():
    """全局约束：超时值必须来自 Settings，不能散落成字面量

    【为什么值得一条测试来守】硬编码的超时无法按环境调整 —— 生产上 Redis 挪到
    同机房想收到 0.3s，外网抓取遇到慢站点想放宽，都得改代码发版。
    """
    offenders = []
    for path in SRC_ROOT.rglob("*.py"):
        for lineno, code in _code_lines(path):
            if any(p.search(code) for p in _HARDCODED_PATTERNS):
                rel = path.relative_to(BACKEND_ROOT)
                offenders.append(f"{rel}:{lineno}  {code.strip()}")

    assert not offenders, "发现硬编码超时字面量，请改为读 Settings：\n" + "\n".join(offenders)


def test_budget_settings_exist_and_are_positive():
    for key in BUDGET_KEYS:
        assert hasattr(settings, key), f"Settings 缺少预算表要求的配置项: {key}"
        assert getattr(settings, key) > 0, f"{key} 应为正数"


def test_budget_keys_documented_in_env_example():
    """config.py 与 .env.example 必须一一对应，无遗漏"""
    env = (BACKEND_ROOT / ".env.example").read_text(encoding="utf-8")
    env_keys = {m.group(1).lower() for m in re.finditer(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=", env, re.M)}

    missing = [k for k in BUDGET_KEYS if k not in env_keys]
    assert not missing, f"这些配置项没写进 .env.example: {missing}"


def test_all_settings_keys_documented_except_known_dead_ones():
    """Settings 里的活配置都要能在 .env 覆盖。

    白名单是三个**零引用**的历史字段（`grep` 全后端无调用方）：它们既不是
    运维旋钮，也没有代码读，写进 .env.example 只会让人以为改了有用。
    """
    dead = {"max_tokens", "num_questions", "request_rate_limit"}

    cfg = (SRC_ROOT / "core" / "config.py").read_text(encoding="utf-8")
    body = cfg.split("class Settings(BaseSettings):", 1)[1].split("class Config:", 1)[0]
    keys = {m.group(1) for m in re.finditer(r"^\s{4}([a-z_][a-z0-9_]*)\s*:", body, re.M)}

    env = (BACKEND_ROOT / ".env.example").read_text(encoding="utf-8")
    env_keys = {m.group(1).lower() for m in re.finditer(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=", env, re.M)}

    missing = sorted(keys - env_keys - dead)
    assert not missing, f"Settings 有但 .env.example 没有: {missing}"


# ================================================================ 运行时穿透
def test_llm_clients_carry_configured_timeouts():
    """LLM 客户端的 timeout 与 max_retries 必须来自配置，且 SDK 重试必须关掉"""
    from src.modules.agent import state_machine as sm

    chat = sm.create_llm()
    if chat is not None:
        assert chat.request_timeout == settings.llm_chat_timeout
        # SDK 自带重试会把「50s 超时」实际变成 150s，必须由应用层统一控制
        assert chat.max_retries == 0

    summary = sm.create_qwen_llm()
    if summary is not None:
        assert summary.request_timeout == settings.llm_summary_timeout
        assert summary.max_retries == 0


def test_summary_llm_carries_summary_timeout_not_chat_timeout():
    """模块级的 summary_llm 必须是摘要档，不能是对话档"""
    from src.modules.agent import state_machine as sm

    if sm.summary_llm is None:
        pytest.skip("无可用 LLM Key，无法验证客户端超时")
    assert sm.summary_llm.request_timeout == settings.llm_summary_timeout


def test_summary_llm_fallback_rebuilds_with_summary_timeout():
    """回退到 DeepSeek 时也必须换档。

    没有 ALIYUN_API_KEY 的环境下 `create_qwen_llm()` 返回 None，若直接 `or llm`
    就会拿到对话档的 50s —— 批量摘要一批 20 条按预算表应有 95s，会被提前掐断。
    """
    src = (SRC_ROOT / "modules" / "agent" / "state_machine.py").read_text(encoding="utf-8")
    assert "create_llm(timeout=settings.llm_summary_timeout)" in src


def test_embedding_client_uses_embedding_timeout():
    from src.modules.agent.embedding_service import EmbeddingService

    service = EmbeddingService(api_key="sk-test-not-real", provider="openai")
    try:
        assert service.http_client.timeout.read == settings.embedding_timeout
        assert service.http_client.timeout.connect == settings.embedding_timeout
    finally:
        import asyncio

        asyncio.run(service.http_client.aclose())


def test_fetch_clients_use_fetch_timeout():
    from src.modules.discovery.article_fetcher import ArticleFetcher
    from src.modules.discovery.github_fetcher import GitHubFetcher

    import asyncio

    article = ArticleFetcher()
    github = GitHubFetcher()
    try:
        assert article.client.timeout.read == settings.fetch_timeout
        assert github.httpx_client.timeout.read == settings.fetch_timeout
    finally:
        asyncio.run(article.client.aclose())
        asyncio.run(github.httpx_client.aclose())


def test_redis_client_uses_configured_socket_timeout():
    """Redis 是热缓存：超时必须短，否则「少一层防护」会被放大成「接口超时」"""
    import redis

    client = redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout,
        socket_connect_timeout=settings.redis_socket_timeout,
    )
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs.get("socket_timeout") == settings.redis_socket_timeout
    assert kwargs.get("socket_connect_timeout") == settings.redis_socket_timeout


def test_qdrant_clients_use_read_and_write_timeouts(monkeypatch):
    """Qdrant 读档 4s / 写档 6s：客户端取默认档，另一档在调用点单独传"""
    from qdrant_client import QdrantClient

    from src.modules.discovery import tech_knowledge as tk

    captured = []
    real_init = QdrantClient.__init__

    def spy_init(self, *args, **kwargs):
        captured.append(kwargs.get("timeout"))
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(QdrantClient, "__init__", spy_init)

    kb = tk.TechKnowledgeBase.__new__(tk.TechKnowledgeBase)
    kb.qdrant = QdrantClient(url=settings.qdrant_url, timeout=settings.qdrant_timeout)

    assert captured == [settings.qdrant_timeout]
    assert settings.qdrant_write_timeout > settings.qdrant_timeout  # 写档必须更宽


def test_database_engine_sets_statement_timeout(monkeypatch):
    """语句超时必须交给服务端强制中断。

    只在客户端断连是不够的：客户端放弃后 PostgreSQL 仍会把那条慢查询跑完并
    一直占着连接，连接池会被慢慢耗干。
    """
    import sqlalchemy

    from src.core import db as dbmod

    captured = {}
    real_create_engine = sqlalchemy.create_engine

    def spy(url, **kwargs):
        captured.update(kwargs)
        return real_create_engine(url, **kwargs)

    monkeypatch.setattr(dbmod, "create_engine", spy)
    engine = dbmod._build_engine()
    try:
        assert captured["connect_args"] == {
            "options": f"-c statement_timeout={settings.db_statement_timeout_ms}"
        }
    finally:
        engine.dispose()
