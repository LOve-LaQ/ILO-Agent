# Services - 卡片检索
"""按 id 取卡片的统一入口（三级降级：内置示例 → 知识库 → Redis 抓取缓存）。

此前这段三级查找逻辑只存在于 `learning.py` 的私有函数里。收藏列表、卡片溯源
都要用同一套语义（尤其是「知识库不可用时也别让页面空掉」），因此收敛到服务层，
避免三处各自演化出不一样的行为。
"""

from typing import Any, Dict, Iterable, List, Optional

from loguru import logger


def _fallback_items() -> List[Dict[str, Any]]:
    """内置示例卡（延迟导入：路由模块导入服务，服务反向导入路由会成环）"""
    try:
        from src.api.routes.discover import FALLBACK_ARTICLES, FALLBACK_NEWS

        return list(FALLBACK_NEWS) + list(FALLBACK_ARTICLES)
    except Exception as e:  # noqa: BLE001 - 示例数据取不到不该阻断检索
        logger.warning(f"[WARN] 读取内置示例卡片失败: {e}")
        return []


def _feed_cache() -> List[Dict[str, Any]]:
    """Redis 抓取缓存（知识库降级时卡片仍在这里）"""
    try:
        from src.api.routes.discover import get_feed_cache

        return get_feed_cache() or []
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 读取抓取缓存失败: {e}")
        return []


def _knowledge_base():
    try:
        from src.modules.discovery.tech_knowledge import get_knowledge_base

        return get_knowledge_base()
    except Exception as e:  # noqa: BLE001 - Qdrant 未启动时退化为无知识库
        logger.warning(f"[WARN] 知识库不可用: {e}")
        return None


def find_card(item_id: str) -> Optional[Dict[str, Any]]:
    """按 id 找一张卡片；找不到返回 None（不编造占位数据）"""
    if not item_id:
        return None

    for item in _fallback_items():
        if item.get("id") == item_id:
            return item

    kb = _knowledge_base()
    if kb is not None:
        try:
            found = kb.get_by_id(item_id)
            if found:
                return found
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WARN] 知识库查询卡片失败: {item_id} - {e}")

    for item in _feed_cache():
        if item.get("id") == item_id:
            return item

    return None


def find_cards(item_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """批量按 id 找卡片，返回 {item_id: card}；找不到的 id 不出现在结果里。

    知识库走一次批量查询；其余来源（示例数据、Redis 缓存）体量都很小，
    直接在内存里过滤，避免为了「统一」而把批量查询拆成 N 次网络往返。
    """
    ids = [str(i) for i in item_ids if i]
    if not ids:
        return {}

    found: Dict[str, Dict[str, Any]] = {}

    pool = _fallback_items() + _feed_cache()
    wanted = set(ids)
    for item in pool:
        item_id = item.get("id")
        if item_id in wanted and item_id not in found:
            found[item_id] = item

    missing = [i for i in ids if i not in found]
    if missing:
        kb = _knowledge_base()
        if kb is not None:
            try:
                found.update(kb.get_by_ids(missing) or {})
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WARN] 知识库批量查询卡片失败: {e}")

    return found


__all__ = ["find_card", "find_cards"]
