# Core - 角色定义
"""角色取值的唯一真相源。

【为什么单独成文件】'admin' 这种字面量一旦散落在依赖、路由、schema 里，
改名或新增角色（operator / auditor）就得全局搜索替换，且漏一处就是权限漏洞。
这里集中定义 + 校验函数，其它模块只引用常量。

【为什么是字符串而不是布尔 is_admin】布尔值只能表达「是/不是管理员」，
再加一个角色就得再来一列。字符串列 + 白名单校验足够，且迁移成本为零。
"""

from typing import Iterable, Optional

ROLE_USER = "user"
ROLE_ADMIN = "admin"

VALID_ROLES = frozenset({ROLE_USER, ROLE_ADMIN})
DEFAULT_ROLE = ROLE_USER


def normalize_role(value: Optional[str]) -> str:
    """把任意输入归一成合法角色；非法值一律降级为普通用户。

    降级而不是抛错：这里读的是数据库列，历史脏数据（或从外部导入的行）
    不该让整个鉴权链路 500。越权判断永远按最保守的角色处理。
    """
    if value in VALID_ROLES:
        return value
    return DEFAULT_ROLE


def parse_emails(raw: str) -> set[str]:
    """把逗号分隔的邮箱配置解析成小写集合（去空白、忽略空项）"""
    if not raw:
        return set()
    items: Iterable[str] = raw.split(",")
    return {item.strip().lower() for item in items if item.strip()}


def effective_role(user, admin_emails: str = "") -> str:
    """计算用户的实际角色。

    优先级：配置白名单 > 数据库列。
    【为什么配置优先】ADMIN_EMAILS 是「部署者意志」的兜底通道：万一管理员误把
    自己的角色改掉、或角色列被批量重置，配置里那个人依然能进后台把权限改回来。
    没有这条通道，一次误操作就会把管理入口永久锁死。
    """
    email = (getattr(user, "email", "") or "").strip().lower()
    if email and email in parse_emails(admin_emails):
        return ROLE_ADMIN
    return normalize_role(getattr(user, "role", None))


def is_admin(user, admin_emails: str = "") -> bool:
    return effective_role(user, admin_emails) == ROLE_ADMIN
