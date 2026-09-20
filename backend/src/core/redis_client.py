# Redis - 统一客户端
"""Redis 客户端（阶段 5：限流 / 登录失败计数 / 会话撤销名单共用）。

为什么要收敛到一处：此前 `collection_service` 与 `discover` 各自
`redis.Redis.from_url(...)`，安全设施再各来一份就是四处重复。

**不可用时返回 None**，降级策略交给调用方决定：
- 限流、登录失败计数：fail-open（放行 + 告警）。本地没起 Redis 时不能让注册
  登录直接变成不可用，否则「跑不起来」比「少一层防护」严重得多；
- 会话撤销名单：跳过检查，退化为「最多一个 access token 有效期」的窗口。

`decode_responses=True`：限流计数与 jti/sid 都是字符串，拿到 bytes 反而处处要 decode。
"""

from typing import Optional

from loguru import logger

from src.core.config import settings

_client: Optional[object] = None
_unavailable_logged = False


def get_redis():
    """获取 Redis 客户端；不可用（未安装 / 连不上 / 超时）返回 None。

    只探测并告警一次：限流是每请求都走的路径，Redis 挂掉时不能每请求刷一条
    告警日志，否则日志会被淹没、真正的错误反而被埋掉。
    """
    global _client, _unavailable_logged
    if _client is not None:
        return _client

    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            # 超时必须短：Redis 不可达时若按默认值等待，请求会卡住数秒，
            # 把「少一层防护」放大成「整个接口超时」。
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
        )
        client.ping()
        _client = client
        _unavailable_logged = False
        return _client
    except Exception as e:  # noqa: BLE001 - 任何异常都只意味着「降级」
        if not _unavailable_logged:
            logger.warning(f"[WARN] Redis 不可用，限流与会话撤销将降级运行: {e}")
            _unavailable_logged = True
        return None


def set_redis_client(client) -> None:
    """测试注入（fakeredis）用；传 None 表示「视为不可用」"""
    global _client, _unavailable_logged
    _client = client
    _unavailable_logged = client is None


def reset_redis_client() -> None:
    """丢弃当前客户端，下次调用重新探测"""
    global _client, _unavailable_logged
    _client = None
    _unavailable_logged = False


__all__ = ["get_redis", "reset_redis_client", "set_redis_client"]
