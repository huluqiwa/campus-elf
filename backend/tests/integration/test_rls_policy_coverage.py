"""RLS 覆盖度守卫：新增租户级表必须带 tenant_id 且启用 RLS + 策略。

这是防"新表漏策略"的门禁（M1-A 设计目标之一）：任何带 `tenant_id` 列的表，
都必须 ENABLE + FORCE ROW LEVEL SECURITY 且存在策略，否则测试失败。
"""

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.db import make_test_engine, upgrade_test_database

pytestmark = [pytest.mark.integration, pytest.mark.business]

# 用 pg_catalog 而非 information_schema.columns：后者只显示当前角色有权限的列，
# 会漏掉"有 tenant_id 但无权限"的表（审查 Minor 2）。
SQL_TENANT_COLUMNS = text(
    """
    select c.relname as table_name,
           case when a.attnotnull then 'NO' else 'YES' end as is_nullable
    from pg_attribute a
    join pg_class c on c.oid = a.attrelid
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'r'
      and a.attname = 'tenant_id' and a.attnum > 0 and not a.attisdropped
    order by c.relname
    """
)

SQL_RLS_STATE = text(
    """
    select c.relname as table_name,
           c.relrowsecurity as rls_enabled,
           c.relforcerowsecurity as rls_forced,
           (select count(*) from pg_policies p
            where p.schemaname = 'public' and p.tablename = c.relname) as policy_count
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'r'
    """
)

# 允许不带 tenant_id 的表（平台级/目录表）；除此之外所有 public 表都必须带 tenant_id。
# 新增业务表若忘了 tenant_id，这里会失败（防"新表漏隔离"）。
TABLES_ALLOWED_WITHOUT_TENANT_ID = {
    "tenants",
    "platform_users",
    "platform_roles",
    "platform_user_roles",
    "platform_refresh_tokens",
    "platform_password_history",
    "platform_password_reset_tokens",
    "platform_mfa_recovery_codes",
    "permissions",
    "platform_role_permissions",
    "tenant_registration_requests",
    "school_review_records",
    "alembic_version",
}

# 必须启用 RLS 的表 = 带 tenant_id 的表 + tenants（按 id 隔离）
EXTRA_RLS_TABLES = {"tenants"}

# 平台级表：允许带**可空** tenant_id（租户开通后回填/平台事件），不要求租户 RLS
PLATFORM_TABLES_WITH_OPTIONAL_TENANT = {
    "audit_logs",
    "tenant_registration_requests",
    "school_review_records",
}


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    """确保测试库结构最新。"""

    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL 未设置")
    upgrade_test_database()
    yield


@pytest.fixture
async def engine(migrated_database: None) -> AsyncIterator[AsyncEngine]:
    """**租户侧应用角色**引擎（RLS 生效）。"""

    test_engine = make_test_engine("tenant")
    yield test_engine
    await test_engine.dispose()


@pytest.fixture
async def platform_engine(migrated_database: None) -> AsyncIterator[AsyncEngine]:
    """**平台侧应用角色**引擎。"""

    test_engine = make_test_engine("platform")
    yield test_engine
    await test_engine.dispose()


@pytest.mark.asyncio
async def test_every_tenant_table_has_forced_rls_and_policy(engine: AsyncEngine) -> None:
    """所有带 tenant_id 的表：tenant_id NOT NULL、RLS 开启且 FORCE、至少一条策略。"""

    async with engine.connect() as conn:
        tenant_columns = (await conn.execute(SQL_TENANT_COLUMNS)).mappings().all()
        rls_rows = (await conn.execute(SQL_RLS_STATE)).mappings().all()

    assert tenant_columns, "未发现任何 tenant_id 列，疑似迁移未执行"
    rls_by_table = {row["table_name"]: row for row in rls_rows}

    problems: list[str] = []
    # 租户级表 = 带 tenant_id 的表 - 平台表（平台表的 tenant_id 可空且无租户 RLS）
    checked_tables = [
        row["table_name"]
        for row in tenant_columns
        if row["table_name"] not in PLATFORM_TABLES_WITH_OPTIONAL_TENANT
    ] + sorted(EXTRA_RLS_TABLES)
    for table in checked_tables:
        meta = rls_by_table.get(table)
        if meta is None:
            problems.append(f"{table} 不在 pg_class 中")
            continue
        if not meta["rls_enabled"] or not meta["rls_forced"]:
            problems.append(f"{table} 未启用 FORCE RLS（表属主可绕过）")
        if meta["policy_count"] == 0:
            problems.append(f"{table} 没有 RLS 策略")

    for row in tenant_columns:
        if (
            row["is_nullable"] != "NO"
            and row["table_name"] not in PLATFORM_TABLES_WITH_OPTIONAL_TENANT
        ):
            problems.append(f"{row['table_name']}.tenant_id 允许 NULL（租户级表必须 NOT NULL）")

    assert not problems, "租户级表 RLS 覆盖不全：\n" + "\n".join(problems)


@pytest.mark.asyncio
async def test_platform_tables_do_not_carry_tenant_column(engine: AsyncEngine) -> None:
    """平台级表不应带 tenant_id（否则会被误当租户表）。"""

    async with engine.connect() as conn:
        rows = (await conn.execute(SQL_TENANT_COLUMNS)).mappings().all()
        all_tables = (
            (
                await conn.execute(
                    text(
                        """
                    select c.relname as table_name from pg_class c
                    join pg_namespace n on n.oid = c.relnamespace
                    where n.nspname = 'public' and c.relkind = 'r'
                    """
                    )
                )
            )
            .mappings()
            .all()
        )

    with_tenant = {row["table_name"] for row in rows}
    unexpected = with_tenant & (
        TABLES_ALLOWED_WITHOUT_TENANT_ID - PLATFORM_TABLES_WITH_OPTIONAL_TENANT
    )
    assert not unexpected, f"平台级表意外带 tenant_id：{unexpected}"

    # 反向守卫：任何 public 表要么带 tenant_id，要么在允许清单里（防新业务表漏列）
    missing_tenant_column = (
        {row["table_name"] for row in all_tables} - with_tenant - TABLES_ALLOWED_WITHOUT_TENANT_ID
    )
    assert not missing_tenant_column, (
        f"以下表既无 tenant_id 也不在平台表清单中，疑似漏了租户列：{missing_tenant_column}"
    )


@pytest.mark.asyncio
async def test_audit_logs_requires_context_for_insert(
    engine: AsyncEngine, platform_engine: AsyncEngine
) -> None:
    """审计表：租户连接无上下文写入被拒；平台连接可写 tenant_id 为空的事件。

    注意：`app.platform_context` 只对**平台角色**的策略生效（策略按角色限定），
    因此平台事件必须走平台连接。"""

    insert_sql = text(
        """
        insert into audit_logs (
          event_id, actor_realm, action, resource_type, result,
          prev_hash, hash, created_at
        ) values (
          gen_random_uuid(), 'system', :action, 'test', 'success',
          repeat('0', 64), repeat('1', 64), now()
        )
        """
    )

    async with engine.begin() as conn:
        with pytest.raises(DBAPIError):
            await conn.execute(insert_sql, {"action": "test.no_context"})

    async with platform_engine.begin() as conn:
        await conn.execute(text("select set_config('app.platform_context', 'on', true)"))
        await conn.execute(insert_sql, {"action": "test.platform"})


@pytest.mark.asyncio
async def test_every_policy_references_context_variables(engine: AsyncEngine) -> None:
    """策略表达式必须引用 `app.tenant_id` 或 `app.platform_context`（防 `USING (true)` 式放行）。"""

    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("select tablename, policyname, qual, with_check from pg_policies")
                )
            )
            .mappings()
            .all()
        )

    assert rows, "没有任何策略"
    bad: list[str] = []
    for row in rows:
        combined = f"{row['qual'] or ''} {row['with_check'] or ''}"
        if "app.tenant_id" not in combined and "app.platform_context" not in combined:
            bad.append(f"{row['tablename']}.{row['policyname']}")
    assert not bad, f"以下策略未引用上下文变量，可能放行一切：{bad}"
