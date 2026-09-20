# Login Guard - 登录失败计数与锁定
"""阶段 5：撞库防护（账号维度）。

**为什么不能只靠 IP 限流**：攻击者用一个 IP 对单个账号试 5 次就换 IP，IP 维度
拦不住；反过来，大量用户共享出口 IP（公司 NAT、校园网）时，IP 维度一收紧就会
误伤一整栋楼。所以 IP 维度管「广度」，账号维度管「对单一目标的深度」，两者互补。

**锁定话术绝不能出现「账号已锁定」**：那等于向未认证的调用方确认「这个账号存在」，
把登录接口变成账号枚举器。对存在与不存在的账号，锁定响应必须一模一样。

计数键用标识的哈希而不是明文：Redis 里不该出现用户邮箱/用户名。
"""

import hashlib
from typing import Optional

from loguru import logger

from src.core.config import settings
from src.core.redis_client import get_redis

KEY_PREFIX = "login:fail"

# 账号标识哈希保留 24 个 hex 字符：这里只做分桶，不需要抗碰撞强度
SUBJECT_HASH_LENGTH = 24


def _subject_key(identifier: Optional[str]) -> str:
    """账号维度计数键。规范化（去空格 + 小写）后再哈希，避免 `Foo@x.com` 与
    `foo@x.com ` 被当成两个不同的桶绕开锁定。"""
    normalized = (identifier or "").strip().lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:SUBJECT_HASH_LENGTH]
    return f"{KEY_PREFIX}:{digest}"


def _lockout_seconds() -> int:
    return max(1, settings.login_lockout_minutes) * 60


def current_failures(identifier: Optional[str]) -> int:
    """读取当前失败次数（Redis 不可用时返回 0，即视为未锁定）"""
    if not identifier:
        return 0
    client = get_redis()
    if client is None:
        return 0
    try:
        value = client.get(_subject_key(identifier))
        return int(value) if value else 0
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 读取登录失败计数失败，按未锁定处理: {e}")
        return 0


def is_locked(identifier: Optional[str]) -> tuple[bool, int]:
    """是否已锁定，返回 (是否锁定, 剩余秒数)"""
    if not identifier:
        return False, 0
    threshold = max(1, settings.login_max_failures)
    if current_failures(identifier) < threshold:
        return False, 0

    client = get_redis()
    if client is None:
        return False, 0
    try:
        ttl = int(client.ttl(_subject_key(identifier)))
        # ttl 为 -1/-2 表示键没有过期时间或已不存在：前者是不该出现的状态，
        # 这里回退成完整锁定时长，避免「锁定窗口无限长」把用户永久挡在门外。
        return True, ttl if ttl > 0 else _lockout_seconds()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 读取锁定状态失败，按未锁定处理: {e}")
        return False, 0


def record_failure(identifier: Optional[str]) -> int:
    """记一次失败，返回累计次数。

    TTL 只在首次计数时设置：每次失败都刷新 TTL 会让「持续低速尝试」永远不过期，
    等于把临时锁定变成永久锁定，正常用户打错几次就再也进不来。
    """
    if not identifier:
        return 0
    client = get_redis()
    if client is None:
        return 0
    try:
        key = _subject_key(identifier)
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        count, ttl = pipe.execute()
        count = int(count)
        if int(ttl) < 0:
            client.expire(key, _lockout_seconds())
        return count
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 记录登录失败次数失败（不影响本次响应）: {e}")
        return 0


def clear_failures(identifier: Optional[str]) -> None:
    """登录成功后清零：否则用户历史上的失败会一直累积到触发锁定"""
    if not identifier:
        return
    client = get_redis()
    if client is None:
        return
    try:
        client.delete(_subject_key(identifier))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WARN] 清零登录失败计数失败: {e}")


__all__ = ["clear_failures", "current_failures", "is_locked", "record_failure"]
