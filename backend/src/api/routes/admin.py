# Admin Routes - 管理后台 API
"""阶段 6：管理员可运行时管理「功能开关」与「用户角色」。

- GET   /admin/features             : 列出全部功能开关（含默认值）
- PATCH /admin/features/{key}       : 开 / 关某个功能模块
- GET   /admin/users                : 用户列表（含角色）
- PATCH /admin/users/{id}/role      : 调整某个用户的角色

契约：
- 全部要求 **admin 角色**（见 api/deps.py 的 require_admin）
- 开关改动**立即生效**：`is_enabled` 每次直读数据库，不做进程内缓存，
  否则演示时会出现「明明关了却还在跑」
- 角色只能改「不是自己」的账号：允许管理员给自己降权，等于给系统留了一个
  「最后一个管理员把自己锁在门外」的死局
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Query

from src.api.deps import AdminUser, DbSession
from src.core.config import settings
from src.core.errors import ERROR_RESPONSES, ILOException
from src.core.roles import ROLE_ADMIN, VALID_ROLES, effective_role, parse_emails
from src.models.user import User
from src.schemas.admin import (
    AdminUserItem,
    AdminUserListResponse,
    AdminUserRoleUpdateRequest,
    FeatureFlagItem,
    FeatureFlagListResponse,
    FeatureFlagUpdateRequest,
    LlmUsageResponse,
)
from src.services.feature_flags import (
    FLAG_DEFINITIONS,
    list_flags,
    set_flag,
)
from src.services.llm_cost import build_usage_report

router = APIRouter(prefix="/admin", tags=["Admin"], responses=ERROR_RESPONSES)

# 由配置白名单强制提权的账号：改数据库角色不会生效，必须明确指出，
# 否则管理员会以为「按钮坏了」
_WHITELIST_HINT = (
    "该账号由 ADMIN_EMAILS 配置白名单强制为管理员，改角色不会生效。"
    "如需调整，请修改 backend/.env 后重启服务。"
)


@router.get("/features", response_model=FeatureFlagListResponse)
def get_feature_flags(user: AdminUser, db: DbSession) -> FeatureFlagListResponse:
    """列出全部功能开关。

    即使某个开关从未被改过也会出现在列表里（值取自代码默认值）——
    管理员需要看到「全貌」，而不是「只有被改过的那几个」。
    """
    items = list_flags(db)
    return FeatureFlagListResponse(items=items, count=len(items))


@router.patch("/features/{key}", response_model=FeatureFlagItem)
def update_feature_flag(
    key: str, data: FeatureFlagUpdateRequest, user: AdminUser, db: DbSession
) -> FeatureFlagItem:
    """开启 / 关闭一个功能模块。

    `key` 必须是 `services/feature_flags.py` 里注册过的开关：允许写任意 key
    等于给数据库开了一个可以无限堆积垃圾行的写入口，而管理页永远只会展示
    已知的那几个。
    """
    if key not in FLAG_DEFINITIONS:
        raise ILOException(
            "FEATURE_UNKNOWN",
            f"未知的功能开关：{key}。",
            status_code=404,
            detail={"known": sorted(FLAG_DEFINITIONS)},
        )

    return FeatureFlagItem(**set_flag(db, key, data.enabled, user.id))


@router.get("/users", response_model=AdminUserListResponse)
def get_users(
    user: AdminUser,
    db: DbSession,
    limit: int = Query(50, ge=1, le=200, description="返回条数上限"),
    offset: int = Query(0, ge=0, description="跳过条数"),
    search: Optional[str] = Query(None, description="按邮箱或用户名模糊搜索"),
) -> AdminUserListResponse:
    """用户列表（按注册时间倒序）。

    只返回 id / 邮箱 / 用户名 / 角色 / 状态 —— 不含 password_hash、
    注销令牌、IP 哈希等任何敏感列。后台是权限最大的界面，
    泄露面必须最小。
    """
    query = db.query(User)
    if search:
        pattern = f"%{search.strip()}%"
        query = query.filter(User.email.ilike(pattern) | User.username.ilike(pattern))

    total = query.count()
    rows = (
        query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()
    )

    items = [
        AdminUserItem(
            id=str(row.id),
            email=row.email,
            username=row.username,
            role=effective_role(row, settings.admin_emails),
            is_active=row.is_active,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return AdminUserListResponse(items=items, count=total)


@router.patch("/users/{user_id}/role", response_model=AdminUserItem)
def update_user_role(
    user_id: UUID, data: AdminUserRoleUpdateRequest, user: AdminUser, db: DbSession
) -> AdminUserItem:
    """调整某个用户的角色。

    两道拒绝：
    - 改自己：防止最后一个管理员把自己降权，从此再也进不了后台；
    - 白名单账号：配置强制为 admin，改库无效，直接说明而不是假装成功。
    """
    role = (data.role or "").strip().lower()
    if role not in VALID_ROLES:
        raise ILOException(
            "INVALID_ROLE",
            f"不支持的角色：{data.role}。",
            status_code=400,
            detail={"allowed": sorted(VALID_ROLES)},
        )

    if user_id == user.id:
        raise ILOException(
            "CANNOT_CHANGE_OWN_ROLE",
            "不能修改自己的角色，请让另一位管理员操作。",
            status_code=409,
        )

    target = db.get(User, user_id)
    if target is None:
        raise ILOException("USER_NOT_FOUND", "目标用户不存在。", status_code=404)

    if (target.email or "").strip().lower() in parse_emails(settings.admin_emails):
        raise ILOException("ROLE_MANAGED_BY_CONFIG", _WHITELIST_HINT, status_code=409)

    target.role = role
    db.commit()
    db.refresh(target)

    return AdminUserItem(
        id=str(target.id),
        email=target.email,
        username=target.username,
        role=effective_role(target, settings.admin_emails),
        is_active=target.is_active,
        created_at=target.created_at,
    )


@router.get("/whoami", response_model=AdminUserItem)
def whoami(user: AdminUser) -> AdminUserItem:
    """当前管理员自述：前端据此确认「后台入口该不该显示」。

    单独开一个端点而不是复用 /auth/me：`/auth/me` 匿名也能调（返回 401 由前端处理），
    而这里能走到 body 就说明「角色校验已通过」，语义更明确。
    """
    return AdminUserItem(
        id=str(user.id),
        email=user.email,
        username=user.username,
        role=ROLE_ADMIN,
        is_active=user.is_active,
        created_at=user.created_at,
    )


@router.get("/llm/usage", response_model=LlmUsageResponse)
def get_llm_usage(
    user: AdminUser,
    db: DbSession,
    days: int = Query(14, ge=1, le=90, description="统计窗口天数（含今天）"),
) -> LlmUsageResponse:
    """LLM 成本：用量、花费、用途/模型拆分与未来 7 天预测。

    【为什么和功能开关放在同一个 router】它们服务同一个决策 ——「这台机器现在
    该不该继续花钱」。开关是**止血的手**，成本面板是**判断该不该止血的眼睛**；
    分到两个页面会让管理员在两个标签页之间来回对照。

    【为什么预测放在后端】预测依赖明细行与价目表，前端拿不到原始 token；
    更重要的是口径必须唯一：如果前端自己再算一遍「日均 × 30」，两个数字会打架，
    而管理人只会相信更小的那个。
    """
    report = build_usage_report(db, days=days, budget_cny=settings.llm_monthly_budget_cny)
    return LlmUsageResponse(**report)
