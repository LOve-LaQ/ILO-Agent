# RAG Module - __init__.py
"""检索增强生成：切分（`chunker`）、块规格（`chunk_spec`）、块向量库（`chunk_store`）、
检索（`retriever`）。

【为什么这里只导出 `chunk_markdown`，其余子模块要显式 import 全路径】
本包内部互相引用（`retriever` → `chunk_store` → `chunk_spec`）。如果在 `__init__`
里把 `retriever` 也 import 进来，那么**任何人** import 本包的任一子模块都会触发
整条链：`__init__` → `retriever` → `chunk_store` → `sqlalchemy` / `qdrant_client`。
后果是「只想用切分器」的离线脚本也被迫拉起数据库和向量库的依赖，而循环导入的
风险也随之出现（`__init__` 执行到一半时子模块回头 import 本包）。

所以约定：**子模块一律全路径 import**，例如
`from src.modules.rag.retriever import search`。
`chunk_markdown` 是唯一例外 —— 它是被外部调用最多的入口，且不依赖任何下游。
"""

from src.modules.rag.chunker import chunk_markdown

__all__ = ["chunk_markdown"]
