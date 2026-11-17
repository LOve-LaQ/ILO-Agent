# Agent Module - __init__.py
"""学习 Agent 模块（状态机 / 记忆 / 上下文 / 向量化）。

【为什么这里不再直接 import 各个类】曾经这里是 `from .state_machine import ...`，
后果是「只要导入这个包里的任意子模块，就会把 state_machine 一并拉起来」——
而 state_machine 顶层 `import langchain_openai`，后者又连带导入
`transformers` + `torch`，实测单这一条冷启动链就要 **15 秒**。

首页个性化只需要 `embedding_service`（一个 httpx 客户端），却因为包的 __init__
被迫等整条 LLM 栈加载完 —— 表现就是「重启后第一次登录，首页转很久」。

改成 PEP 562 惰性导出后：
- `from src.modules.agent import LearningStateMachine` 依然可用（首次访问时导入）；
- `import src.modules.agent.embedding_service` 不再拖起 langchain / torch。
"""

from importlib import import_module

# 类名 / 函数名 → 所在子模块
_LAZY_EXPORTS = {
    "LearningStateMachine": "state_machine",
    "LearningState": "state_machine",
    "MemoryManager": "memory_manager",
    "LearningContext": "context",
    "EmbeddingService": "embedding_service",
    "get_embedding_service": "embedding_service",
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name):
    """按需导入（PEP 562）：首次访问属性时才加载对应子模块"""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    # 缓存进 globals()：__getattr__ 每次访问都会被调用，不缓存等于重复走查找
    globals()[name] = value
    return value


def __dir__():
    return sorted(__all__)
