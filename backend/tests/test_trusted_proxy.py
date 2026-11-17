"""X-Forwarded-For 信任边界。

背景：实测发现原实现无条件采信 XFF —— 同一个来源换一个 `X-Forwarded-For` 头，
就拿全新的限流桶，所有按 IP 的配额都能无限绕过。这里把「谁有权注入 XFF」
钉成一条可配置、可回归的边界。
"""

from starlette.requests import Request

from src.core import net
from src.core.config import settings


def _request(client_host: str, headers: dict[str, str] | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [
            (key.lower().encode(), value.encode())
            for key, value in (headers or {}).items()
        ],
        "client": (client_host, 12345),
    }
    return Request(scope)


def test_trusted_local_proxy_xff_is_used(monkeypatch):
    """cpolar / vite proxy 场景：本机收到请求，XFF 首个地址才是真实来源"""
    assert (
        net.client_ip(
            _request("127.0.0.1", {"x-forwarded-for": "203.0.113.7, 127.0.0.1"})
        )
        == "203.0.113.7"
    )


def test_untrusted_peer_xff_is_ignored():
    """公网直连：伪造 XFF 不能换一个限流桶"""
    assert (
        net.client_ip(
            _request("198.51.100.9", {"x-forwarded-for": "8.8.8.8"})
        )
        == "198.51.100.9"
    )


def test_no_xff_falls_back_to_peer():
    assert net.client_ip(_request("203.0.113.4")) == "203.0.113.4"


def test_ipv4_mapped_ipv6_peer_is_trusted():
    """Windows 上本机可能写成 ::ffff:127.0.0.1，必须和 127.0.0.1 等价"""
    assert (
        net.client_ip(
            _request("::ffff:127.0.0.1", {"x-forwarded-for": "203.0.113.11"})
        )
        == "203.0.113.11"
    )


def test_empty_xff_falls_back_to_peer():
    assert net.client_ip(_request("127.0.0.1", {"x-forwarded-for": " , "})) == "127.0.0.1"


def test_custom_proxy_allowlist(monkeypatch):
    """容器前面挂 nginx 时，把那一跳加进白名单即可"""
    monkeypatch.setattr(settings, "trusted_proxy_ips", "172.18.0.2")
    assert (
        net.client_ip(
            _request("172.18.0.2", {"x-forwarded-for": "203.0.113.20"})
        )
        == "203.0.113.20"
    )
    # 同一配置下，本机反而不信了 —— 白名单是精确边界，不是「额外追加」
    assert net.client_ip(_request("127.0.0.1", {"x-forwarded-for": "8.8.8.8"})) == "127.0.0.1"


def test_malformed_allowlist_does_not_break_requests(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_ips", "not-an-ip, ,127.0.0.1")
    assert (
        net.client_ip(_request("127.0.0.1", {"x-forwarded-for": "203.0.113.30"}))
        == "203.0.113.30"
    )


def test_forged_xff_yields_same_bucket_as_real_ip():
    """端到端断言：伪造头不改变限流键，这是这次修复真正要保证的性质"""
    real = _request("198.51.100.9")
    forged = _request("198.51.100.9", {"x-forwarded-for": "8.8.8.8"})
    assert net.rate_limit_key(net.client_ip(real)) == net.rate_limit_key(
        net.client_ip(forged)
    )
