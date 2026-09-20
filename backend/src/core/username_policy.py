# Username Policy - 用户名规范
"""阶段 5：把「只限长度」的用户名规则补成字符集 + 保留字。

**为什么要限字符集**：用户名会出现在 URL、日志、导出文件与统计报表里。放行任意
Unicode 就意味着要处理同形字（用西里尔字母 `а` 拼出的 `аdmin`）、零宽字符、控制
字符与 RTL 覆盖符 —— 这些在展示与检索时全是坑，而在注册这一道关口限死 ASCII
字母数字加 `_`/`-`，这些问题根本不会出现。

**为什么要保留字**：`admin`、`official`、`system` 这类名字被普通用户占用后，轻则
让人误以为是官方账号（可用于钓鱼与冒充），重则在未来的路由、子域名或邮箱规划上撞车。

**为什么禁首尾与连续分隔符**：`_user_`、`user__name` 在视觉上容易被读错（冒充
他人账号的常见手法），也会让 URL 参数与文件名处理多出一类边界情况。
"""

import re
from typing import Optional

USERNAME_MIN = 2
USERNAME_MAX = 50

# 只允许 ASCII 字母、数字、下划线、连字符
USERNAME_ALLOWED = re.compile(r"^[A-Za-z0-9_-]+$")

# 连续分隔符：`__` / `--` / `_-` / `-_`
USERNAME_REPEATED_SEPARATOR = re.compile(r"[_-]{2}")

# 保留字（大小写不敏感）。宁可多留几个，被占用的代价远大于少留一个。
RESERVED_USERNAMES = frozenset(
    {
        "admin", "administrator", "root", "system", "sysadmin", "superuser",
        "support", "service", "help", "helpdesk", "official", "staff", "team",
        "owner", "moderator", "operator", "security", "abuse", "postmaster",
        "webmaster", "hostmaster", "noreply", "no-reply", "mail", "email",
        "api", "www", "web", "app", "static", "assets", "public", "internal",
        "me", "user", "users", "account", "accounts", "profile", "settings",
        "login", "logout", "register", "signup", "signin", "auth", "oauth",
        "library", "discover", "dashboard", "console", "portal", "home",
        "ilo", "iloagent", "ilo-agent", "agent", "bot", "robot", "crawler",
        "test", "testing", "demo", "example", "sample", "guest", "anonymous",
        "null", "undefined", "none", "nan", "true", "false",
        "billing", "payment", "invoice", "order", "subscribe", "newsletter",
        "legal", "privacy", "terms", "policy", "about", "contact", "blog",
        "news", "docs", "documentation", "status", "health", "metrics",
    }
)


def check_username(value: Optional[str]) -> Optional[str]:
    """校验用户名。通过返回 None，否则返回面向用户的拒绝原因。"""
    candidate = (value or "").strip()

    if len(candidate) < USERNAME_MIN:
        return f"用户名至少需要 {USERNAME_MIN} 个字符。"
    if len(candidate) > USERNAME_MAX:
        return f"用户名最多 {USERNAME_MAX} 个字符。"
    if not USERNAME_ALLOWED.match(candidate):
        return "用户名只能包含字母、数字、下划线和连字符。"
    if candidate[0] in "_-" or candidate[-1] in "_-":
        return "用户名不能以下划线或连字符开头或结尾。"
    if USERNAME_REPEATED_SEPARATOR.search(candidate):
        return "用户名不能包含连续的下划线或连字符。"
    if candidate.lower() in RESERVED_USERNAMES:
        return "该用户名为系统保留，请换一个。"

    return None


__all__ = ["RESERVED_USERNAMES", "USERNAME_MAX", "USERNAME_MIN", "check_username"]
