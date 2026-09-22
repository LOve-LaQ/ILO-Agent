# Test Summary Localization - 卡片简介中文化闸门
"""锁住「卡片简介必须中文化」这条闸门。

背景：`collect_items` 曾经在判断摘要是否成功**之前**就无条件 `store()`，导致 LLM
摘要整批失败时，卡片带着 GitHub 的英文原描述进了知识库并直接展示给用户（实测
65 张卡片里有 6 张）。这里用单测把三道闸门钉住：
- `is_chinese_text`：判据本身
- `_apply_summary`：LLM 输出非中文时不得回写 summary
- `_summary_applied`：入库前的最终校验必须同时看 category 与语言
"""

from src.modules.discovery.github_fetcher import _apply_summary
from src.modules.discovery.summary_spec import is_chinese_text
from src.services.collection_service import _summary_applied


# ---------------------------------------------------------------- 判据

def test_english_description_is_not_chinese():
    assert not is_chinese_text(
        "Apple Wallet Card Skinner for iOS 18+ (No Jailbreak Required)"
    )
    assert not is_chinese_text("Z.ai's coding agent harness. Powerful, intelligent.")


def test_empty_text_is_not_chinese():
    assert not is_chinese_text("")
    assert not is_chinese_text("   ")
    assert not is_chinese_text(None)


def test_chinese_summary_is_chinese():
    assert is_chinese_text(
        "AirCard 是一款面向 iOS 18+ 的 Apple Wallet 卡片皮肤工具，无需越狱即可自定义卡片外观。"
    )


def test_chinese_with_tech_terms_is_still_chinese():
    """技术简介天然夹带大量英文术语，判据按汉字数量而非占比，不得误杀"""
    assert is_chinese_text(
        "WP2Shell PoC 是 WordPress 到 Shell 的 RCE 漏洞验证工具，覆盖 CVE-2026-60130。"
    )
    assert is_chinese_text(
        "基于 Kubernetes 与 Docker 的部署方案，用 Redis 做缓存、Qdrant 做向量检索。"
    )


# ---------------------------------------------------------------- 应用期闸门

def _english_leak_item():
    """模拟摘要失败时的卡片：summary 上还挂着 GitHub 的英文原描述"""
    return {
        "id": "gh-1",
        "title": "Mak5er/AirCard",
        "summary": "Apple Wallet Card Skinner for iOS 18+ (No Jailbreak Required)",
        "raw_description": "Apple Wallet Card Skinner for iOS 18+",
    }


def test_apply_summary_rejects_english_output():
    item = _english_leak_item()
    _apply_summary(item, {"category": "mobile", "summary": "Apple Wallet Card Skinner for iOS 18+"})

    # 英文输出必须被丢弃：不能留下 category，也不能把英文留在 summary 上
    assert "category" not in item
    assert "summary" not in item
    assert not _summary_applied(item), "非中文卡片必须判为「未完成」而不能入库"
    # 原描述要保住，供溯源使用
    assert item["raw_description"]


def test_apply_summary_keeps_raw_description_when_rejecting():
    item = {"id": "gh-2", "title": "x/y", "summary": "Some English description"}
    _apply_summary(item, {"summary": "Still English"})
    assert item.get("summary") is None
    assert item["raw_description"] == "Some English description"


def test_apply_summary_accepts_chinese_output():
    item = _english_leak_item()
    _apply_summary(
        item,
        {
            "category": "mobile",
            "summary": "AirCard 是一款面向 iOS 18+ 的 Apple Wallet 卡片皮肤工具，无需越狱即可自定义卡片外观。",
            "one_liner": "iOS 无需越狱的 Apple Wallet 换肤工具",
            "tech_stack": ["Swift", "iOS 18"],
        },
    )

    assert item["category"] == "mobile"
    assert is_chinese_text(item["summary"])
    assert _summary_applied(item)


def test_apply_summary_accepts_chinese_with_tech_terms():
    item = {"id": "gh-3", "title": "a/b", "summary": "English material"}
    _apply_summary(
        item,
        {
            "category": "security",
            "summary": "WP2Shell PoC 是 WordPress 到 Shell 的 RCE 漏洞验证工具，覆盖 CVE-2026-60130。",
            "one_liner": "WordPress RCE 漏洞验证 PoC",
        },
    )
    assert item["category"] == "security"
    assert _summary_applied(item)


# ---------------------------------------------------------------- 入库前最终校验

def test_summary_applied_rejects_category_with_english_summary():
    """这是英文卡片漏进知识库的那条路径：category 有、summary 却是英文"""
    assert not _summary_applied(
        {
            "category": "mobile",
            "summary": "Apple Wallet Card Skinner for iOS 18+ (No Jailbreak Required)",
        }
    )


def test_summary_applied_requires_category():
    assert not _summary_applied({"summary": "这是一段合格的中文简介，讲清了它是什么。"})


def test_summary_applied_accepts_valid_card():
    assert _summary_applied(
        {"category": "mobile", "summary": "这是一段合格的中文简介，讲清了它是什么、解决什么问题。"}
    )
