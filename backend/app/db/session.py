"""数据库会话与租户上下文注入。

**关键约束（`docs/design-briefs/M1-tenant-identity.md` §3.1）**：
- 每个使用租户数据的请求都必须在一个事务内设置 `app.tenant_id`；
- 使用 `set_config(..., is_local=true)`（等价 `SET LOCAL`），事务结束自动失效，
  避免连接池复用导致跨请求串租户；
- **平台路径与租户路径使用不同的数据库连接**（`campus_elf_platform` / `campus_elf_tenant`
  两个非超级用户角色）：平台请求显式 `platform=True`，用平台连接读审计表；
  租户请求在数据库层就拿不到平台表权限，隔离不依赖应用层纪律。
- 连接角色必须是**非超级用户**：超级用户连 FORCE RLS 也绕过。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engines: dict[str, AsyncEngine] = {}
_factories: dict[str, async_sessionmaker[AsyncSession]] = {}


def migration_url_or_default() -> str:
    """返回迁移用连接串（优先 `MIGRATION_DATABASE_URL`，否则 `DATABASE_URL`）。

    异常:
        RuntimeError: 两者都未配置。
    """

    settings = get_settings()
    if settings.migration_database_url is not None:
        return settings.migration_database_url.get_secret_value()
    if settings.database_url:
        return settings.database_url
    raise RuntimeError("MIGRATION_DATABASE_URL / DATABASE_URL 均未配置")


def _make_engine(url: str, *, pool_size: int = 10) -> AsyncEngine:
    """创建异步引擎（统一参数）。"""

    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
    )


def get_engine(*, platform: bool = False) -> AsyncEngine:
    """返回进程内缓存的异步引擎（租户/平台各一份）。

    参数:
        platform: True 取平台连接（`PLATFORM_DATABASE_URL`），否则取租户连接（`DATABASE_URL`）。

    返回值:
        AsyncEngine。

    异常:
        RuntimeError: 对应连接串未配置。
    """

    settings = get_settings()
    if platform:
        url = settings.platform_database_url
        if not url:
            raise RuntimeError("PLATFORM_DATABASE_URL 未配置；平台路径必须用独立的非超级用户连接")
        key = "platform"
    else:
        url = settings.database_url
        if not url:
            raise RuntimeError("DATABASE_URL 未配置；数据库相关功能不可用")
        key = "tenant"

    if key not in _engines:
        _engines[key] = _make_engine(url)
    return _engines[key]


def get_session_factory(*, platform: bool = False) -> async_sessionmaker[AsyncSession]:
    """返回对应角色的会话工厂。"""

    key = "platform" if platform else "tenant"
    if key not in _factories:
        _factories[key] = async_sessionmaker(get_engine(platform=platform), expire_on_commit=False)
    return _factories[key]


async def dispose_engine() -> None:
    """关闭全部引擎（测试与进程退出时调用）。"""

    for engine in list(_engines.values()):
        await engine.dispose()
    _engines.clear()
    _factories.clear()


def normalize_tenant_id(tenant_id: UUID | str | None) -> str | None:
    """把租户标识规范化为 UUID 字符串。

    None/空串表示不使用租户上下文；非法值直接报错（由调用方转 401/403），
    避免把类型错误留给数据库（会变成 500）。

    异常:
        ValueError: 非空但无法解析为 UUID。
    """

    if tenant_id is None:
        return None
    value = str(tenant_id).strip()
    if not value:
        return None
    return str(UUID(value))


@asynccontextmanager
async def db_session(
    *, tenant_id: UUID | str | None = None, platform: bool = False
) -> AsyncIterator[AsyncSession]:
    """打开一个事务会话并按需注入租户/平台上下文。

    参数:
        tenant_id: 租户上下文（租户请求必填；缺失时 RLS fail-closed，查不到任何行）。
        platform: 平台级代码路径显式置 True（使用平台连接 + `app.platform_context`）。
            **与 tenant_id 互斥**：同时传入属编程错误，直接报错，避免租户请求读到全部审计。

    返回值:
        处于事务中的 AsyncSession。

    异常:
        ValueError: platform 与 tenant_id 同时传入，或 tenant_id 非法。
        RuntimeError: 对应连接串未配置。
    """

    if platform and tenant_id is not None:
        raise ValueError("platform=True 与 tenant_id 互斥：平台路径不得带租户上下文")

    normalized = normalize_tenant_id(tenant_id)
    factory = get_session_factory(platform=platform)
    # 审计哈希链密钥：在事务内注入 GUC（触发器读取），事务结束自动失效。
    # 注意：密钥经绑定参数传入；若本语句异常，SQLAlchemy 异常 repr 可能带
    # [parameters: ...]（含密钥）——调用方不得把该异常原样写入日志（M6 日志脱敏收口）。
    audit_key = get_settings().audit_hash_key

    async with factory() as session, session.begin():
        if audit_key is not None:
            await session.execute(
                text("select set_config('app.audit_hash_key', :key, true)"),
                {"key": audit_key.get_secret_value()},
            )
        if platform:
            await session.execute(
                text("select set_config('app.platform_context', 'on', true)")
            )
        if normalized is not None:
            await session.execute(
                text("select set_config('app.tenant_id', :tid, true)"),
                {"tid": normalized},
            )
        yield session
