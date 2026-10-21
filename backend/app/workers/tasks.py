"""Worker 任务定义。"""

from typing import Any
from uuid import UUID


async def echo(ctx: dict[str, Any], value: str) -> str:
    """回显任务，用于 Arq/Redis 连通性冒烟。

    参数:
        ctx: Arq 任务上下文。
        value: 要回显的字符串。

    返回值:
        原样返回 value。

    异常:
        无。
    """

    return value


# 过期/已撤销刷新令牌的保留期（天）：到期后清理，避免表无限增长（TD-067）
REFRESH_TOKEN_RETENTION_DAYS = 30


async def cleanup_refresh_tokens(ctx: dict[str, Any]) -> dict[str, int]:
    """清理过期/已撤销的刷新令牌（TD-067）。

    租户表带 RLS：先经平台连接列出启用租户，再逐个在租户上下文内删除；
    平台表直接删除。保留期内的行不清理（便于异常检测与排障）。

    参数:
        ctx: Arq 任务上下文（未使用）。

    返回值:
        {"tenant_tokens": 清理条数, "platform_tokens": 清理条数}。

    异常:
        无。单租户失败不影响其他租户（逐租户独立事务，异常向上抛出由 Arq 重试）。
    """

    from sqlalchemy import text

    from app.db.session import db_session

    tenant_tokens = 0
    params = {"days": REFRESH_TOKEN_RETENTION_DAYS}

    async with db_session(platform=True) as platform_session:
        tenant_ids: list[UUID] = list(
            (await platform_session.execute(text("select id from tenants"))).scalars().all()
        )
        platform_result = await platform_session.execute(
            text(
                "delete from platform_refresh_tokens "
                "where expires_at < now() - make_interval(days => :days) "
                "   or (revoked_at is not null "
                "       and revoked_at < now() - make_interval(days => :days))"
            ),
            params,
        )

    for tenant_id in tenant_ids:
        async with db_session(tenant_id=tenant_id) as session:
            result = await session.execute(
                text(
                    "delete from refresh_tokens "
                    "where expires_at < now() - make_interval(days => :days) "
                    "   or (revoked_at is not null "
                    "       and revoked_at < now() - make_interval(days => :days))"
                ),
                params,
            )
            tenant_tokens += int(getattr(result, "rowcount", 0) or 0)

    return {
        "tenant_tokens": tenant_tokens,
        "platform_tokens": int(getattr(platform_result, "rowcount", 0) or 0),
    }
