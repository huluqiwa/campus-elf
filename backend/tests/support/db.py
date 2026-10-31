"""数据库测试支撑：迁移、清理与按角色区分的引擎。

三种连接（见 `docs/design.md` §4.6）：
- **admin**（`TEST_DATABASE_URL`）：迁移/清理用，测试库里是超级用户；
- **tenant**（`TEST_APP_DATABASE_URL`）：租户侧运行路径，非超级用户，RLS 生效；
- **platform**（`TEST_PLATFORM_DATABASE_URL`）：平台侧运行路径，非超级用户，可读平台表与审计表。

仅测试使用；**清理助手会临时关闭审计仅追加触发器**（生产禁止任何形式的绕过）。
"""

import os
from pathlib import Path
from typing import Any, Literal

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]

# 允许清理的租户级表（顺序：先子表后主表）
TENANT_TABLES = (
    # 子表在前：kb_document_versions → kb_documents → kb_upload_batches → import_row_errors → ...
    "kb_document_versions",
    "kb_documents",
    "kb_upload_batches",
    "import_row_errors",
    "import_jobs",
    "departments",  # teachers.department_id 引用，先清部门再清教师
    "teacher_class_courses",
    "students",
    "teachers",
    "classes",
    "courses",
    "user_roles",
    "role_permissions",
    "data_scopes",
    "refresh_tokens",
    "password_reset_tokens",
    "password_history",
    "mfa_recovery_codes",
    "roles",
    "users",
)

EngineRole = Literal["admin", "tenant", "platform"]

_ENV_BY_ROLE: dict[EngineRole, str] = {
    "admin": "TEST_DATABASE_URL",
    "tenant": "TEST_APP_DATABASE_URL",
    "platform": "TEST_PLATFORM_DATABASE_URL",
}


def database_url(role: EngineRole) -> str:
    """返回指定角色的连接串。

    异常:
        RuntimeError: 对应环境变量未设置。
    """

    env_name = _ENV_BY_ROLE[role]
    url = os.environ.get(env_name)
    if not url:
        raise RuntimeError(f"{env_name} 未设置（先运行 scripts/test-stack.sh up 并导出 TEST_*）")
    return url


def upgrade_test_database() -> None:
    """对测试库执行 Alembic 迁移到 head（同步调用，供 session 级 fixture 使用）。"""

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    # 通过 -x db_url=... 传给 env.py，避免改动全局 settings
    config.cmd_opts = type("Args", (), {"x": [f"db_url={database_url('admin')}"]})()
    command.upgrade(config, "head")


def make_test_engine(role: EngineRole = "admin") -> AsyncEngine:
    """创建指定角色的异步引擎。

    参数:
        role: admin（迁移/清理）、tenant（应用租户路径）、platform（平台路径）。

    返回值:
        AsyncEngine。
    """

    return create_async_engine(database_url(role), pool_size=5, max_overflow=5)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """创建会话工厂。"""

    return async_sessionmaker(engine, expire_on_commit=False)


async def assert_role_is_not_superuser(engine: AsyncEngine) -> None:
    """断言连接角色不是超级用户且没有 BYPASSRLS（否则 RLS 形同虚设）。

    异常:
        AssertionError: 连接角色具备绕过隔离的权限。
    """

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    """
                    select current_user as role_name, rolsuper, rolbypassrls
                    from pg_roles where rolname = current_user
                    """
                )
            )
        ).one()
    assert row.rolsuper is False, f"连接角色 {row.role_name} 是超级用户，RLS 会被绕过"
    assert row.rolbypassrls is False, f"连接角色 {row.role_name} 带 BYPASSRLS，RLS 会被绕过"


async def prepare_tenant_context(session: AsyncSession, tenant_id: Any | None) -> None:
    """在事务内设置租户上下文（等价应用层 `db_session(tenant_id=...)`）。"""

    if tenant_id is None:
        await session.execute(text("select set_config('app.tenant_id', '', true)"))
    else:
        await session.execute(
            text("select set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )


async def prepare_platform_context(session: AsyncSession) -> None:
    """在事务内设置平台上下文。"""

    await session.execute(text("select set_config('app.platform_context', 'on', true)"))


async def clean_test_data(engine: AsyncEngine) -> None:
    """清理测试数据（仅测试用；含临时关闭审计触发器）。

    生产环境禁止任何关闭审计仅追加触发的操作；此处仅为让测试可重复运行。
    使用 try/finally 保证触发器一定恢复（异常时不留下"仅追加已禁用"状态）。
    """

    async with engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_no_update_delete")
        )
        await conn.execute(text("ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_no_truncate"))
        try:
            await conn.execute(text("TRUNCATE audit_logs"))
            await conn.execute(
                text("TRUNCATE school_review_records, tenant_registration_requests CASCADE")
            )
            for table in TENANT_TABLES:
                await conn.execute(text(f"TRUNCATE {table} CASCADE"))
            await conn.execute(text("TRUNCATE platform_user_roles CASCADE"))
            # M1-E2：策略开关表引用 platform_users，必须排在它之前
            await conn.execute(text("TRUNCATE system_settings CASCADE"))
            await conn.execute(text("TRUNCATE platform_users CASCADE"))
            await conn.execute(text("TRUNCATE tenants CASCADE"))
        finally:
            await conn.execute(
                text("ALTER TABLE audit_logs ENABLE TRIGGER audit_logs_no_update_delete")
            )
            await conn.execute(text("ALTER TABLE audit_logs ENABLE TRIGGER audit_logs_no_truncate"))
