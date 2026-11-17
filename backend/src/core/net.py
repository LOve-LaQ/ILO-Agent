# Net - 请求来源信息
"""客户端 IP 的提取与脱敏（阶段 5：入口加固 + 个人信息保护）。

**提取**：只有在直连方是可信代理时才采信 `X-Forwarded-For` 首个地址。
经 vite 代理 / cpolar 隧道 / nginx 时 `request.client.host` 是代理地址（127.0.0.1），
拿它做限流等于全站共用一个计数桶；XFF 由代理追加，取第一个才是原始客户端。
但无条件采信 XFF 是实打实的漏洞：任何人换个请求头就换一个限流桶，实测可无限绕过。
白名单见 `settings.trusted_proxy_ips`，默认只信本机，支持 CIDR 网段
（容器部署时把 Docker 网桥网段加进来，否则 XFF 被忽略、全站共用一个桶）。

**脱敏**：落库前一律走 `hash_ip()`。为什么不能存明文、也不能用无盐哈希：
- 明文 IP 属个人信息，库被拖走就等于泄露访问者的网络位置；
- 无盐哈希对 IPv4 毫无意义 —— 地址空间只有 2^32，彩虹表秒破。

HMAC 使用独立 secret（`IP_HASH_SECRET`，不复用 `JWT_SECRET`），两套凭证互不牵连，
且不提供任何反查能力。未配置 secret 时宁可不记录，也不落明文。
"""

import hashlib
import hmac
import ipaddress
from functools import lru_cache
from typing import Optional

from src.core.config import settings

# 落库哈希保留 32 个 hex 字符（128 bit）：足够抗碰撞，又比完整 64 位短一半
STORED_HASH_LENGTH = 32

# 限流键用的指纹短一些即可：只做分桶，不需要抗碰撞强度
RATE_LIMIT_KEY_LENGTH = 24


def _normalize_ip(value: str) -> Optional[str]:
    """规范化 IP 文本；解析不了就返回 None。

    Windows 上 IPv4 映射地址会写成 `::ffff:127.0.0.1`，不归一化的话它会和白名单里的
    `127.0.0.1` 比不上， tunnel 场景下限流又退回「全站共用一个桶」。
    """
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return str(addr)


@lru_cache(maxsize=8)
def _trusted_networks(raw: str) -> tuple:
    """解析白名单为网段；按原始字符串缓存，改配置后自然拿到新结果。

    支持 CIDR（如 `172.16.0.0/12`）：Docker 网桥地址随宿主而异，写死单个 IP
    会在换了机器后静默失效 —— 那时 XFF 被忽略、全站共用一个限流桶，且没有任何
    报错。无效条目直接跳过：配置写错一个地址不该让全站请求 500。
    """
    result = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return tuple(result)


def _is_trusted_proxy(peer: Optional[str]) -> bool:
    normalized = _normalize_ip(peer or "")
    if not normalized:
        return False
    addr = ipaddress.ip_address(normalized)
    return any(addr in net for net in _trusted_networks(settings.trusted_proxy_ips))


def client_ip(request) -> Optional[str]:
    """真实客户端 IP：直连方可信时才用 XFF 首个地址，否则用直连地址"""
    if request is None:
        return None
    peer = request.client.host if request.client else None
    if _is_trusted_proxy(peer):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    return peer


def _digest(secret: str, value: str, length: int) -> str:
    return hmac.new(
        secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:length]


def hash_ip(ip: Optional[str]) -> Optional[str]:
    """IP -> 落库用的 HMAC 指纹；未配置 secret 时返回 None（宁可不记，不落明文）"""
    if not ip:
        return None
    secret = settings.ip_hash_secret
    if not secret:
        return None
    return _digest(secret, ip, STORED_HASH_LENGTH)


def rate_limit_key(ip: Optional[str]) -> str:
    """限流分桶用的 IP 指纹。

    与 `hash_ip` 的区别：这里**必须有值**，否则所有请求会挤进同一个计数桶，
    要么误伤全站、要么等于没限流。所以未配置 secret 时回退到一个固定 salt ——
    限流键只存在 Redis 且带 TTL，不落库、不长期保留，不构成信息泄露。
    """
    if not ip:
        return "unknown"
    secret = settings.ip_hash_secret or "ilo-rate-limit"
    return _digest(secret, ip, RATE_LIMIT_KEY_LENGTH)


def subject_fingerprint(value: Optional[str]) -> str:
    """非 IP 主体的限流分桶指纹（如登录账号）。

    先规范化（去空格 + 小写）再哈希：否则 `Foo@x.com` 与 `foo@x.com ` 会被
    分成两个桶，攻击者只要变换大小写/空格就能绕开「同账号」维度的限流。
    """
    normalized = (value or "").strip().lower()
    if not normalized:
        return "unknown"
    secret = settings.ip_hash_secret or "ilo-rate-limit"
    return _digest(secret, normalized, RATE_LIMIT_KEY_LENGTH)


__all__ = ["client_ip", "hash_ip", "rate_limit_key", "subject_fingerprint"]
