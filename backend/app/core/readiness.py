"""依赖就绪探活（/health/ready 的判定逻辑）。

对 PostgreSQL / Redis / Qdrant 逐项探活并给出状态：
- `ok`：探活通过；
- `not_configured`：未配置该依赖（开发期允许，生产由配置校验拦截）；
- `error`：探活失败（总体 503，附可读原因；不回显凭证）。
"""

import asyncio
from typing import Literal, NamedTuple

import asyncpg
import httpx
import redis.asyncio as aioredis

from app.config import Settings, get_settings

# 单次探活超时（秒）：探针调用必须快速返回。
CHECK_TIMEOUT_SECONDS = 3.0

CheckStatus = Literal["ok", "not_configured", "error"]


class DependencyCheck(NamedTuple):
    """单个依赖的探活结果。"""

    name: str
    status: CheckStatus
    detail: str = ""


def _plain_postgres_dsn(url: str) -> str:
    """把 SQLAlchemy 风格 DSN 转为 asyncpg 可用的 DSN。"""

    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _check_postgres(settings: Settings) -> DependencyCheck:
    """探活 PostgreSQL（`select 1`）。"""

    if not settings.database_url:
        return DependencyCheck("postgres", "not_configured")
    try:
        connection = await asyncio.wait_for(
            asyncpg.connect(_plain_postgres_dsn(settings.database_url)),
            timeout=CHECK_TIMEOUT_SECONDS,
        )
        try:
            await asyncio.wait_for(connection.fetchval("select 1"), timeout=CHECK_TIMEOUT_SECONDS)
        finally:
            await connection.close()
    except Exception as exc:  # 探活失败原因要可读，但不回显凭证
        return DependencyCheck("postgres", "error", type(exc).__name__)
    return DependencyCheck("postgres", "ok")


async def _check_redis(settings: Settings) -> DependencyCheck:
    """探活 Redis（PING）。

    DSN 非法（缺少 scheme 等）同样归入 error，由就绪检查转成 503，
    而不是让异常冒泡成 500。
    """

    client: aioredis.Redis | None = None
    try:
        # redis-py 的 asyncio.from_url 缺少类型标注（上游未补全），此处显式忽略。
        client = aioredis.from_url(  # type: ignore[no-untyped-call]
            settings.redis_url
        )
        await asyncio.wait_for(client.ping(), timeout=CHECK_TIMEOUT_SECONDS)
    except Exception as exc:
        return DependencyCheck("redis", "error", type(exc).__name__)
    finally:
        if client is not None:
            await client.aclose()
    return DependencyCheck("redis", "ok")


async def _check_qdrant(settings: Settings) -> DependencyCheck:
    """探活 Qdrant（`/healthz`）。"""

    if not settings.qdrant_url:
        return DependencyCheck("qdrant", "not_configured")
    try:
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{settings.qdrant_url.rstrip('/')}/healthz")
            response.raise_for_status()
    except Exception as exc:
        return DependencyCheck("qdrant", "error", type(exc).__name__)
    return DependencyCheck("qdrant", "ok")


async def collect_readiness(settings: Settings | None = None) -> list[DependencyCheck]:
    """并发探活全部依赖。

    参数:
        settings: 可选配置覆盖；默认取进程缓存的配置。

    返回值:
        list[DependencyCheck]：按 postgres/redis/qdrant 顺序的结果。
    """

    resolved = settings if settings is not None else get_settings()
    return list(
        await asyncio.gather(
            _check_postgres(resolved),
            _check_redis(resolved),
            _check_qdrant(resolved),
        )
    )
