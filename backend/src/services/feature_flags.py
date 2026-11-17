# Services - 运行时功能开关
"""管理员可运行时关闭/开启功能模块，无需重启后端。

## 为什么不只用 .env
`.env` 里的 `RAG_CHAT_ENABLED` 这类开关是**进程级**的：改完必须重启，
而重启要等 LLM 客户端初始化（10~20s）。演示现场没有这个时间。

## 三道设计约束
1. **数据库是唯一真相源，代码只留默认值**。行不存在时按默认值处理，
   因此新部署不需要跑种子脚本，也不会出现「漏插一行导致功能默认关闭」。
2. **不做进程内缓存**。一次主键查询是毫秒级，而开关的语义是「点一下立刻生效」。
   加缓存会让演示时出现「明明关了却还在跑」的困惑，收益远小于成本。
3. **数据库不可用时一律视为「开」**。未配置 `DATABASE_URL` 是纯 demo 环境的
   合法状态；开关的职责是「主动关」，不是「缺失即关」——否则一次数据库抖动
   会让全站功能集体消失，那是比被刷单更严重的故障。

## 与限流的关系
限流挡的是「单位时间内的调用量」，开关挡的是「功能整体是否可用」。
两者互补：限流保留可用性，开关给管理员一个**成本熔断**的物理开关。
"""

from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from src.core.db import get_session_factory, is_database_configured
from src.core.errors import ILOException
from src.models.feature_flag import FeatureFlag

# 开关注册表：key -> (默认是否开启, 说明)。
# 新增开关**只需往这里加一行**，管理页与 API 会自动出现，无需改后端路由。
FLAG_DEFINITIONS: Dict[str, tuple[bool, str]] = {
    "discover_refresh": (
        True,
        "抓取 GitHub 热门仓库 / 多平台文章（含批量 LLM 摘要，成本最高）",
    ),
    "card_digest": (
        True,
        "卡片中文导读（每次未命中缓存都会真实调用 LLM）",
    ),
    "learning_chat": (
        True,
        "学习对话、讲解与追问（每轮都是一次 LLM 推理，成本最高的常规接口）",
    ),
}

DISABLED_MESSAGE = "该功能已由管理员关闭，暂时不可用。"


def default_enabled(key: str) -> bool:
    entry = FLAG_DEFINITIONS.get(key)
    return entry[0] if entry else True


def _read(db: Session, key: str, default: bool) -> bool:
    row = db.get(FeatureFlag, key)
    return default if row is None else bool(row.enabled)


def is_enabled(key: str, db: Optional[Session] = None) -> bool:
    """判断某个开关当前是否开启（任何异常都回退到默认值，绝不因开关故障阻断业务）"""
    if key not in FLAG_DEFINITIONS:
        # 代码里写错 key 是开发期错误，但绝不能在运行期把功能锁死
        logger.warning(f"[WARN] 未知功能开关 {key}，按默认开启处理")
        return True

    default = default_enabled(key)

    # 调用方已带会话：直接复用，避免一次请求开两个连接
    if db is not None:
        try:
            return _read(db, key, default)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WARN] 读取开关 {key} 失败，按默认值 {default} 处理: {e}")
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return default

    if not is_database_configured():
        return default

    try:
        with get_session_factory()() as session:
            return _read(session, key, default)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 读取开关 {key} 失败，按默认值 {default} 处理: {e}")
        return default


def require_feature(key: str, db: Optional[Session] = None) -> None:
    """开关关闭时抛 503 FEATURE_DISABLED。

    选 503 而不是 403：这不是「你没权限」，而是「服务当前不提供该能力」。
    403 会让前端误以为是账号问题、引导用户去登录；503 才能让前端展示
    「管理员已关闭该功能」这类准确的提示。
    """
    if is_enabled(key, db):
        return
    raise ILOException(
        "FEATURE_DISABLED",
        DISABLED_MESSAGE,
        status_code=503,
        detail={"feature": key},
    )


def list_flags(db: Session) -> List[Dict[str, Any]]:
    """合并「代码默认值」与「数据库实际值」，供管理页展示"""
    rows = {row.key: row for row in db.query(FeatureFlag).all()}

    items: List[Dict[str, Any]] = []
    for key, (default, description) in FLAG_DEFINITIONS.items():
        row = rows.pop(key, None)
        items.append(
            {
                "key": key,
                "enabled": default if row is None else bool(row.enabled),
                "default_enabled": default,
                "description": description,
                "updated_at": row.updated_at if row is not None else None,
                "updated_by": str(row.updated_by) if row is not None and row.updated_by else None,
            }
        )

    # 库里残留但代码已删除的开关也如实展示：藏起来会让管理员看到一个
    # 「改不掉的开关」，反而更难排查
    for key, row in rows.items():
        items.append(
            {
                "key": key,
                "enabled": bool(row.enabled),
                "default_enabled": True,
                "description": row.description or "（代码中已无此开关定义）",
                "updated_at": row.updated_at,
                "updated_by": str(row.updated_by) if row.updated_by else None,
            }
        )
    return items


def set_flag(db: Session, key: str, enabled: bool, updated_by: Optional[Any]) -> Dict[str, Any]:
    """写入开关（不存在则插入），返回写入后的完整视图"""
    row = db.get(FeatureFlag, key)
    if row is None:
        row = FeatureFlag(
            key=key,
            enabled=enabled,
            description=FLAG_DEFINITIONS.get(key, (True, ""))[1],
            updated_by=updated_by,
        )
        db.add(row)
    else:
        row.enabled = enabled
        row.updated_by = updated_by
    db.commit()
    db.refresh(row)

    logger.info(f"[FLAG] 功能开关 {key} 已{'开启' if enabled else '关闭'}")
    return {
        "key": row.key,
        "enabled": bool(row.enabled),
        "default_enabled": default_enabled(key),
        "description": row.description,
        "updated_at": row.updated_at,
        "updated_by": str(row.updated_by) if row.updated_by else None,
    }
