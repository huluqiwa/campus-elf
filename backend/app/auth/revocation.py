"""访问令牌撤销（登出即时失效）。

刷新令牌的撤销由数据库承担（轮换/家族撤销），访问令牌是无状态 JWT，登出后到过期前
（≤15 分钟）需要在 Redis 里留一条撤销标记。键只含 jti，不含任何身份信息；TTL 与令牌
剩余有效期一致，过期自动清理（不依赖定时任务）。
"""

from datetime import UTC, datetime
from typing import Literal

import redis.asyncio as aioredis

from app.config import get_settings

# 键带 realm 与项目前缀，租户/平台互不干扰；TTL 与令牌剩余有效期一致，自动过期无需清理
_KEY_TEMPLATE = "{prefix}:auth:revoked:{realm}:jti:{jti}"
_Realm = Literal["tenant", "platform"]
_client: aioredis.Redis | None = None
# Redis 异常不得拖垮已鉴权请求：连接/读写超时短且显式设置（TD-102）
_SOCKET_TIMEOUT_SECONDS = 2.0


def _redis() -> aioredis.Redis:
    """返回进程内复用的 Redis 客户端（懒加载）。"""

    global _client
    if _client is None:
        _client = aioredis.from_url(  # type: ignore[no-untyped-call]
            get_settings().redis_url,
            decode_responses=True,
            socket_timeout=_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
        )
    return _client


def _key(jti: str, realm: _Realm) -> str:
    """撤销标记的键名（含项目前缀与 realm）。"""

    return _KEY_TEMPLATE.format(prefix=get_settings().redis_prefix, realm=realm, jti=jti)


async def revoke(
    jti: str, expires_at: datetime | None = None, *, realm: _Realm = "tenant"
) -> None:
    """把访问令牌 jti 加入撤销集合。

    参数:
        jti: 访问令牌唯一标识。
        expires_at: 令牌过期时间；缺省按配置的访问令牌 TTL 计算。
    """

    settings = get_settings()
    if expires_at is None:
        ttl = settings.access_token_ttl_seconds
    else:
        ttl = int((expires_at - datetime.now(UTC)).total_seconds())
        ttl = max(ttl, 1)
    await _redis().set(_key(jti, realm), "1", ex=ttl)


async def is_revoked(jti: str, *, realm: _Realm = "tenant") -> bool:
    """jti 是否已被撤销。"""

    return bool(await _redis().exists(_key(jti, realm)))


async def reset_client() -> None:
    """关闭并清空客户端（测试/进程退出用）。"""

    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
