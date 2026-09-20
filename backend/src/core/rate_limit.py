# Rate Limit - 固定窗口限流
"""入口加固（阶段 5）：注册 / 登录 / 刷新 / 找回密码 / 验证码校验 / 确认邮件重发的节流。

**为什么用固定窗口**：Redis 上 `INCR` + `EXPIRE` 就是一个精确的固定窗口计数器，
不需要 Lua、不需要额外状态，读代码的人一眼能验证它对不对。代价是窗口边界处最多
放行 2 倍配额 —— 而这里的目标是「显著抬高脚本批量注册与撞库的成本」，不是做流量整形。

**为什么 Redis 不可用时 fail-open**：本地开发常常没起 Redis，此时若直接拒绝，
注册和登录会一起失效。那是把「少一层防护」升级成「服务完全不可用」，代价大得多。
所以只告警、不拦，把可用性放在前面。

**限额从 settings 现取**而不是在建规则时固化：测试要能在用例里临时把限额压到 1
而不必重启进程。
"""

from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import Request
from loguru import logger

from src.core.config import settings
from src.core.errors import ILOException
from src.core.net import client_ip, rate_limit_key
from src.core.redis_client import get_redis

# 键前缀：与业务键（session:、crawled:）分开，便于运维目视区分与批量清理
KEY_PREFIX = "rl"

# scope -> (settings 中的限额字段名, 窗口秒数)
_RULES: dict[str, tuple[str, int]] = {
    "register:ip": ("register_rate_limit_per_hour", 3600),
    "login:ip": ("login_rate_limit_ip_per_15min", 900),
    "login:account": ("login_rate_limit_account_per_15min", 900),
    "refresh:ip": ("refresh_rate_limit_per_hour", 3600),
    "forgot:ip": ("forgot_rate_limit_per_hour", 3600),
    "forgot:email": ("forgot_email_rate_limit_per_hour", 3600),
    "captcha:ip": ("captcha_rate_limit_per_hour", 3600),
    "email:resend": ("email_resend_rate_limit_per_hour", 3600),
}


@dataclass(frozen=True)
class RateLimitRule:
    """一条限流规则（只带 scope，限额与窗口在判定时从 settings 现取）"""

    scope: str


def _limits(scope: str) -> tuple[int, int]:
    field_name, window_seconds = _RULES[scope]
    return int(getattr(settings, field_name)), window_seconds


def humanize_seconds(seconds: int) -> str:
    """把秒数说成人话（限流提示要给用户一个可操作的等待时长）"""
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def hit(rule: RateLimitRule, subject: str) -> tuple[bool, int]:
    """记一次命中，返回 (是否放行, 建议重试秒数)

    窗口实现：第一次命中时 `INCR` 返回 1，此时设过期时间；后续命中只自增。
    计数与取 TTL 放进同一个 pipeline，避免两次往返之间窗口刚好过期导致读到 -1。
    """
    if not settings.rate_limit_enabled:
        return True, 0

    client = get_redis()
    if client is None:
        return True, 0  # fail-open，理由见模块 docstring

    limit, window_seconds = _limits(rule.scope)
    if limit <= 0:
        return True, 0  # 配成 0 或负数表示「该规则不生效」，方便临时放行排查

    key = f"{KEY_PREFIX}:{rule.scope}:{subject}"
    try:
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        count, ttl = pipe.execute()
        count = int(count)
        ttl = int(ttl)

        if count == 1 or ttl < 0:
            # 首次命中：窗口从这一刻开始。`ttl < 0` 兜住「键存在但没有过期时间」
            # 的异常状态（例如上一次 expire 失败），否则计数会永远不过期。
            client.expire(key, window_seconds)
            ttl = window_seconds

        if count > limit:
            return False, max(1, ttl)
        return True, 0
    except Exception as e:  # noqa: BLE001 - 限流失败不能连带业务失败
        logger.warning(f"[WARN] 限流计数失败，本次放行: {rule.scope} - {e}")
        return True, 0


def enforce(rule: RateLimitRule, subject: str, *, message: Optional[str] = None) -> None:
    """超限即抛 429（带 Retry-After，前端据此提示还要等多久）"""
    allowed, retry_after = hit(rule, subject)
    if allowed:
        return

    text = message or f"操作过于频繁，请 {humanize_seconds(retry_after)}后再试。"
    raise ILOException(
        "RATE_LIMITED",
        text,
        status_code=429,
        detail={"retry_after": retry_after},
        headers={"Retry-After": str(retry_after)},
    )


def rate_limit(rule: RateLimitRule) -> Callable[[Request], None]:
    """FastAPI 依赖工厂：按客户端 IP 分桶"""

    def _dependency(request: Request) -> None:
        enforce(rule, rate_limit_key(client_ip(request)))

    return _dependency


# 命名规则常量：路由里写 `Depends(rate_limit(REGISTER_IP))`
REGISTER_IP = RateLimitRule("register:ip")
LOGIN_IP = RateLimitRule("login:ip")
LOGIN_ACCOUNT = RateLimitRule("login:account")
REFRESH_IP = RateLimitRule("refresh:ip")
FORGOT_IP = RateLimitRule("forgot:ip")
FORGOT_EMAIL = RateLimitRule("forgot:email")
CAPTCHA_IP = RateLimitRule("captcha:ip")
EMAIL_RESEND_IP = RateLimitRule("email:resend")


__all__ = [
    "CAPTCHA_IP",
    "EMAIL_RESEND_IP",
    "FORGOT_EMAIL",
    "FORGOT_IP",
    "LOGIN_ACCOUNT",
    "LOGIN_IP",
    "REGISTER_IP",
    "REFRESH_IP",
    "RateLimitRule",
    "enforce",
    "hit",
    "humanize_seconds",
    "rate_limit",
]
