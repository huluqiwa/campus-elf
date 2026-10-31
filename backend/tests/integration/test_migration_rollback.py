"""M1-B 迁移回滚回归：存在校方超管数据时可安全 downgrade / 再 upgrade。

审查发现：旧版 downgrade 直接恢复 `user_type` 旧约束，遇到 school_admin 行会失败。
本测试在**独立临时数据库**中复现"先升级、再写入超管、再降级"，避免污染主测试库。
"""

import asyncio
import os
import re
from pathlib import Path
from typing import cast
from uuid import UUID

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from asyncpg import Record
from sqlalchemy.engine import make_url

from tests.support.db import BACKEND_ROOT
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.integration]

M1A_REVISION = "779f731db342"
M1C2_PREVIOUS_REVISION = "c8e2f4a6b1d3"
D1_PREVIOUS_REVISION = "d4e6f8a0b2c1"
D1_REVISION = "b2d4f6a8c0e1"
M1F_PREVIOUS_REVISION = "f7a9b1c3d5e8"
M2_PREVIOUS_REVISION = "a8c3e9f1b5d7"
M2_ORG_TABLES = ("classes", "courses", "teachers", "students", "teacher_class_courses")
M3_KB_PREVIOUS_REVISION = "a1c3e5f7b9d2"
M3_KB_TABLES = ("kb_document_versions", "kb_documents", "kb_upload_batches")
M1F_TEST_KEY = "m1f-rollback-audit-key-0123456789abcdef"


def _alembic_config(url: str) -> Config:
    """构造指向临时库的 Alembic 配置。"""

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.cmd_opts = type("Args", (), {"x": [f"db_url={url}"]})()
    return config


def _admin_dsn(url: str) -> tuple[str, str]:
    """把 async SQLAlchemy URL 转成 asyncpg DSN 与数据库名。"""

    parsed = make_url(url)
    if not parsed.database:
        raise AssertionError(f"测试连接串缺少数据库名: {url}")
    dsn = url.replace("postgresql+asyncpg://", "postgresql://")
    return dsn, parsed.database


async def _create_database(dsn: str, database: str) -> None:
    """创建临时数据库（先清理同名残留）。"""

    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await connection.execute(f'CREATE DATABASE "{database}"')
    finally:
        await connection.close()


async def _drop_database(dsn: str, database: str) -> None:
    """删除临时数据库。"""

    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    finally:
        await connection.close()


async def _seed_school_admin(dsn: str) -> None:
    """写入一个校方超管（模拟 M1-B 开通结果）。"""

    connection = await asyncpg.connect(dsn)
    try:
        tenant_id = await connection.fetchval(
            """
            insert into tenants (school_code, name, status, activated_at)
            values ('SCH-ROLLBACK1', '回滚测试中学', 'active', now())
            returning id
            """
        )
        await connection.execute(
            """
            insert into users (
                tenant_id, user_type, login_id, password_hash,
                status, must_change_password, mfa_enabled
            ) values (
                $1, 'school_admin', 'rollback@school.edu', '$2b$12$fake',
                'active', true, false
            )
            """,
            tenant_id,
        )
    finally:
        await connection.close()


async def _school_admin_count(dsn: str) -> int:
    """统计校方超管数量（按 user_type 过滤，避免未来学员/教师数据干扰）。"""

    connection = await asyncpg.connect(dsn)
    try:
        return int(
            await connection.fetchval("select count(*) from users where user_type = 'school_admin'")
        )
    finally:
        await connection.close()


async def _provision_function_exists(dsn: str) -> int:
    """确认重新升级后开通函数存在。"""

    connection = await asyncpg.connect(dsn)
    try:
        return int(
            await connection.fetchval(
                "select count(*) from pg_proc where proname = 'provision_tenant'"
            )
        )
    finally:
        await connection.close()


async def _assert_constraint_restored(dsn: str) -> None:
    """旧约束恢复后，插入 school_admin 应失败。"""

    connection = await asyncpg.connect(dsn)
    try:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await connection.execute(
                """
                insert into users (
                    tenant_id, user_type, login_id, password_hash,
                    status, must_change_password, mfa_enabled
                )
                select id, 'school_admin', 'x@school.edu', '$2b$12$fake',
                       'active', true, false
                from tenants limit 1
                """
            )
    finally:
        await connection.close()


def test_downgrade_with_school_admin_rows_then_upgrade() -> None:
    """有超管数据时 downgrade 删除其账号并恢复约束，随后可重新 upgrade。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)

    dsn, database = _admin_dsn(admin_url)
    temp_database = f"{database}_rollback"
    temp_dsn = dsn.rsplit("/", 1)[0] + f"/{temp_database}"
    temp_url = admin_url.rsplit("/", 1)[0] + f"/{temp_database}"

    asyncio.run(_create_database(dsn, temp_database))
    try:
        config = _alembic_config(temp_url)
        command.upgrade(config, "head")
        asyncio.run(_seed_school_admin(temp_dsn))

        command.downgrade(config, M1A_REVISION)

        assert asyncio.run(_school_admin_count(temp_dsn)) == 0
        asyncio.run(_assert_constraint_restored(temp_dsn))

        command.upgrade(config, "head")
        assert asyncio.run(_provision_function_exists(temp_dsn)) == 1
    finally:
        asyncio.run(_drop_database(dsn, temp_database))


def test_migration_test_helpers_are_isolated() -> None:
    """临时库名必须以主测试库为前缀（防止误删非测试库）。"""

    parsed = make_url("postgresql+asyncpg://u:p@127.0.0.1:5432/campus_elf_test")
    assert parsed.database is not None and parsed.database.endswith("_test")
    assert Path(__file__).name == "test_migration_rollback.py"


async def _simulate_first_round_schema(dsn: str) -> None:
    """模拟**真实第一轮** c688：无内部分通函数、无硬化触发器，平台有开通 INSERT 与策略。"""

    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(
            "DROP TRIGGER IF EXISTS school_review_records_no_update_delete ON school_review_records"
        )
        await connection.execute(
            "DROP TRIGGER IF EXISTS tenant_registration_requests_status_guard "
            "ON tenant_registration_requests"
        )
        await connection.execute(
            "DROP FUNCTION IF EXISTS review_registration_request("
            "uuid, text, text, uuid, text, uuid, text, uuid, text, text)"
        )
        await connection.execute("DROP FUNCTION IF EXISTS provision_tenant(jsonb)")
        for table in ("roles", "users", "user_roles", "role_permissions"):
            await connection.execute(f"GRANT INSERT ON {table} TO campus_elf_platform")
            await connection.execute(f"DROP POLICY IF EXISTS platform_provision ON {table}")
            await connection.execute(
                f"CREATE POLICY platform_provision ON {table} FOR INSERT "
                "TO campus_elf_platform "
                "WITH CHECK (current_setting('app.platform_context', true) = 'on')"
            )
        await connection.execute(
            "GRANT UPDATE ON tenant_registration_requests TO campus_elf_platform"
        )
        await connection.execute("GRANT INSERT ON school_review_records TO campus_elf_platform")
    finally:
        await connection.close()


async def _assert_hardened(dsn: str, platform_dsn: str) -> None:
    """确认硬化后的权限/函数/触发器（含分通函数对 PUBLIC/平台不可执行）。"""

    connection = await asyncpg.connect(dsn)
    try:
        privileges = await connection.fetchrow(
            """
            select has_table_privilege('campus_elf_platform', 'users', 'INSERT') as users_insert,
                   has_table_privilege('campus_elf_platform', 'users', 'UPDATE') as users_update,
                   has_table_privilege('campus_elf_platform', 'roles', 'INSERT') as roles_insert,
                   has_table_privilege('campus_elf_platform',
                                       'tenant_registration_requests', 'UPDATE') as request_update,
                   has_table_privilege('campus_elf_platform',
                                       'school_review_records', 'INSERT') as record_insert,
                   has_table_privilege('campus_elf_platform', 'tenants', 'INSERT') as tenant_insert,
                   has_function_privilege('campus_elf_platform',
                                          'provision_tenant(jsonb)', 'EXECUTE') as provision_exec,
                   has_function_privilege('campus_elf_platform',
                                          'review_registration_request('
                                          'uuid, text, text, uuid, text, uuid, text, uuid, text, text)',
                                          'EXECUTE') as review_exec,
                   has_function_privilege('campus_elf_tenant',
                                          'review_registration_request('
                                          'uuid, text, text, uuid, text, uuid, text, uuid, text, text)',
                                          'EXECUTE') as tenant_review_exec
            """
        )
        assert dict(privileges) == {
            "users_insert": False,
            "users_update": False,
            "roles_insert": False,
            "request_update": False,
            "record_insert": False,
            "tenant_insert": False,
            "provision_exec": False,
            "review_exec": True,
            "tenant_review_exec": False,
        }
        # ACL 文本中 PUBLIC 授权表现为空前缀的 "=X/"（如 "{=X/owner,...}"）
        provision_acl = await connection.fetchval(
            "select proacl::text from pg_proc where proname = 'provision_tenant'"
        )
        review_acl = await connection.fetchval(
            "select proacl::text from pg_proc where proname = 'review_registration_request'"
        )
        public_grant = re.compile(r"(^|[{,])=X/")
        assert provision_acl is not None and not public_grant.search(provision_acl), provision_acl
        assert review_acl is not None and not public_grant.search(review_acl), review_acl

        policies = await connection.fetchval(
            "select count(*) from pg_policies where policyname = 'platform_provision'"
        )
        assert policies == 0
        function_exists = await connection.fetchval(
            "select count(*) from pg_proc where proname = 'review_registration_request'"
        )
        triggers = await connection.fetch(
            "select tgname from pg_trigger where tgname in "
            "('school_review_records_no_update_delete', "
            " 'tenant_registration_requests_status_guard')"
        )
        assert function_exists == 1
        assert len(triggers) == 2
    finally:
        await connection.close()

    # 平台连接直接调用分通函数必须被拒绝（B4）
    platform_connection = await asyncpg.connect(platform_dsn)
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await platform_connection.execute("select provision_tenant('{}'::jsonb)")
    finally:
        await platform_connection.close()


def test_hardening_revision_repairs_first_round_schema() -> None:
    """M20：从第一轮 revision 升到 head 时，硬化 revision 会补齐函数/触发器并收回权限。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)

    dsn, database = _admin_dsn(admin_url)
    platform_url = os.environ.get("TEST_PLATFORM_DATABASE_URL")
    if not platform_url:
        pytest.skip("TEST_PLATFORM_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up")
    temp_database = f"{database}_legacy"
    temp_dsn = dsn.rsplit("/", 1)[0] + f"/{temp_database}"
    temp_url = admin_url.rsplit("/", 1)[0] + f"/{temp_database}"
    temp_platform_dsn = (
        platform_url.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]
        + f"/{temp_database}"
    )

    asyncio.run(_create_database(dsn, temp_database))
    try:
        config = _alembic_config(temp_url)
        command.upgrade(config, "c688c44dec81")
        asyncio.run(_simulate_first_round_schema(temp_dsn))

        command.upgrade(config, "head")

        asyncio.run(_assert_hardened(temp_dsn, temp_platform_dsn))

        # 从旧 schema 升级后再降级：必须成功，且回滚不得扩大权限、不得重开 B1（M25/M26/B5）
        command.downgrade(config, "c688c44dec81")
        asyncio.run(_assert_c688_state(temp_dsn, temp_platform_dsn))
    finally:
        asyncio.run(_drop_database(dsn, temp_database))


async def _assert_c688_state(dsn: str, platform_dsn: str) -> None:
    """降级到 c688：身份表无任何平台权限、无开通策略，且平台不能向存量租户写账号。"""

    connection = await asyncpg.connect(dsn)
    try:
        row = await connection.fetchrow(
            """
            select has_table_privilege('campus_elf_platform', 'users', 'INSERT') as users_insert,
                   has_table_privilege('campus_elf_platform', 'users', 'UPDATE') as users_update,
                   has_table_privilege('campus_elf_platform', 'roles', 'INSERT') as roles_insert,
                   has_table_privilege('campus_elf_platform',
                                       'tenant_registration_requests', 'UPDATE') as req_update,
                   has_table_privilege('campus_elf_platform', 'tenants', 'INSERT') as tenant_insert,
                   has_function_privilege('campus_elf_platform',
                                          'provision_tenant(jsonb)', 'EXECUTE') as provision_exec
            """
        )
        assert dict(row) == {
            "users_insert": False,
            "users_update": False,
            "roles_insert": False,
            "req_update": True,
            "tenant_insert": True,
            "provision_exec": True,
        }
        policies = await connection.fetchval(
            "select count(*) from pg_policies where policyname = 'platform_provision'"
        )
        assert policies == 0
        review_function = await connection.fetchval(
            "select count(*) from pg_proc where proname = 'review_registration_request'"
        )
        assert review_function == 0
        # 造一个存量租户，供越权插入断言使用
        await connection.execute(
            "insert into tenants (school_code, name, status) "
            "values ('SCH-C688CHK', '降级校验中学', 'active')"
        )
    finally:
        await connection.close()

    platform_connection = await asyncpg.connect(platform_dsn)
    try:
        # 同一显式事务内设置平台上下文，忠实复现"GUC 已设且策略存在"的攻击环境
        async with platform_connection.transaction():
            await platform_connection.execute(
                "select set_config('app.platform_context', 'on', true)"
            )
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await platform_connection.execute(
                    """
                    insert into users (
                        tenant_id, user_type, login_id, password_hash,
                        status, must_change_password, mfa_enabled
                    )
                    select id, 'school_admin', 'backdoor@evil.test', '$2b$12$fake',
                           'active', true, false
                    from tenants limit 1
                    """
                )
    finally:
        await platform_connection.close()


async def _add_permissive_policy(dsn: str) -> None:
    """模拟误加宽松策略：users 上增加 USING (true)（M24）。"""

    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(
            "CREATE POLICY r3_permissive ON users USING (true) WITH CHECK (true)"
        )
    finally:
        await connection.close()


async def _disable_users_force_rls(dsn: str) -> None:
    """模拟误操作：users 关闭 FORCE RLS（且表为空，空表 count 无法暴露问题）。"""

    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute("ALTER TABLE users NO FORCE ROW LEVEL SECURITY")
    finally:
        await connection.close()


def test_selfcheck_detects_disabled_rls_on_empty_table() -> None:
    """B2：users 关闭 FORCE RLS 且为空时，启动自检必须失败（不能靠 count=0 取证）。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    tenant_url = os.environ.get("TEST_APP_DATABASE_URL")
    platform_url = os.environ.get("TEST_PLATFORM_DATABASE_URL")
    if not (admin_url and tenant_url and platform_url):
        pytest.skip("TEST_* 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)

    dsn, database = _admin_dsn(admin_url)
    temp_database = f"{database}_rlscheck"
    temp_dsn = dsn.rsplit("/", 1)[0] + f"/{temp_database}"
    temp_url = admin_url.rsplit("/", 1)[0] + f"/{temp_database}"
    temp_tenant_url = tenant_url.rsplit("/", 1)[0] + f"/{temp_database}"
    temp_platform_url = platform_url.rsplit("/", 1)[0] + f"/{temp_database}"

    asyncio.run(_create_database(dsn, temp_database))
    saved = {key: os.environ.get(key) for key in ("DATABASE_URL", "PLATFORM_DATABASE_URL")}
    try:
        command.upgrade(_alembic_config(temp_url), "head")
        asyncio.run(_disable_users_force_rls(temp_dsn))

        from app.config import get_settings
        from app.db.selfcheck import DatabaseSelfCheckError, verify_runtime_database
        from app.db.session import dispose_engine

        os.environ["DATABASE_URL"] = temp_tenant_url
        os.environ["PLATFORM_DATABASE_URL"] = temp_platform_url
        get_settings.cache_clear()
        asyncio.run(dispose_engine())

        with pytest.raises(DatabaseSelfCheckError, match="users"):
            asyncio.run(verify_runtime_database())
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        from app.config import get_settings
        from app.db.session import dispose_engine

        get_settings.cache_clear()
        asyncio.run(dispose_engine())
        asyncio.run(_drop_database(dsn, temp_database))


def test_selfcheck_detects_permissive_policy_on_users() -> None:
    """M24：users 上出现 USING (true) 宽松策略时，启动自检必须失败。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    tenant_url = os.environ.get("TEST_APP_DATABASE_URL")
    platform_url = os.environ.get("TEST_PLATFORM_DATABASE_URL")
    if not (admin_url and tenant_url and platform_url):
        pytest.skip("TEST_* 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)

    dsn, database = _admin_dsn(admin_url)
    temp_database = f"{database}_policycheck"
    temp_dsn = dsn.rsplit("/", 1)[0] + f"/{temp_database}"
    temp_url = admin_url.rsplit("/", 1)[0] + f"/{temp_database}"
    temp_tenant_url = tenant_url.rsplit("/", 1)[0] + f"/{temp_database}"
    temp_platform_url = platform_url.rsplit("/", 1)[0] + f"/{temp_database}"

    asyncio.run(_create_database(dsn, temp_database))
    saved = {key: os.environ.get(key) for key in ("DATABASE_URL", "PLATFORM_DATABASE_URL")}
    try:
        command.upgrade(_alembic_config(temp_url), "head")
        asyncio.run(_add_permissive_policy(temp_dsn))

        from app.config import get_settings
        from app.db.selfcheck import DatabaseSelfCheckError, verify_runtime_database
        from app.db.session import dispose_engine

        os.environ["DATABASE_URL"] = temp_tenant_url
        os.environ["PLATFORM_DATABASE_URL"] = temp_platform_url
        get_settings.cache_clear()
        asyncio.run(dispose_engine())

        with pytest.raises(DatabaseSelfCheckError, match="策略"):
            asyncio.run(verify_runtime_database())
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        from app.config import get_settings
        from app.db.session import dispose_engine

        get_settings.cache_clear()
        asyncio.run(dispose_engine())
        asyncio.run(_drop_database(dsn, temp_database))


async def _seed_m1c2_reasons(dsn: str) -> None:
    """写入租户/平台刷新令牌行，覆盖 rotated 与两种会话撤销原因。"""

    connection = await asyncpg.connect(dsn)
    try:
        tenant_id = await connection.fetchval(
            "insert into tenants (school_code, name, status, activated_at) "
            "values ('SCH-M1C2ROLL', '会话回滚中学', 'active', now()) returning id"
        )
        user_id = await connection.fetchval(
            "insert into users (tenant_id, user_type, login_id, password_hash, "
            "status, must_change_password, mfa_enabled) "
            "values ($1, 'student', 'm1c2-rollback@school.edu', '$2b$12$fake', "
            "'active', false, false) returning id",
            tenant_id,
        )
        for index, reason in enumerate(("rotated", "session_kicked", "session_revoked", "logout")):
            await connection.execute(
                "insert into refresh_tokens (tenant_id, user_id, jti, family_id, "
                "token_hash, expires_at, last_seen_at, revoked_at, revoked_reason) "
                "values ($1, $2, gen_random_uuid(), gen_random_uuid(), $3, "
                "now() + interval '1 day', now(), now(), $4)",
                tenant_id,
                user_id,
                f"{index:064x}",
                reason,
            )

        platform_user_id = await connection.fetchval(
            "insert into platform_users (account, password_hash, status, "
            "must_change_password, mfa_enabled) "
            "values ('m1c2-rollback-ops', '$2b$12$fake', 'active', false, false) "
            "returning id"
        )
        for index, reason in enumerate(("rotated", "session_kicked", "session_revoked", "logout")):
            await connection.execute(
                "insert into platform_refresh_tokens (platform_user_id, jti, family_id, "
                "token_hash, expires_at, last_seen_at, revoked_at, revoked_reason) "
                "values ($1, gen_random_uuid(), gen_random_uuid(), $2, "
                "now() + interval '1 day', now(), now(), $3)",
                platform_user_id,
                f"{index + 10:064x}",
                reason,
            )
    finally:
        await connection.close()


async def _m1c2_rollback_state(dsn: str) -> tuple[list[str], list[str], list[str], str]:
    """返回两表撤销原因、新列名与 last_seen_at 可空性。"""

    connection = await asyncpg.connect(dsn)
    try:
        tenant_reasons = [
            str(row["revoked_reason"])
            for row in await connection.fetch(
                "select revoked_reason from refresh_tokens order by token_hash"
            )
        ]
        platform_reasons = [
            str(row["revoked_reason"])
            for row in await connection.fetch(
                "select revoked_reason from platform_refresh_tokens order by token_hash"
            )
        ]
        columns = [
            f"{row['table_name']}.{row['column_name']}"
            for row in await connection.fetch(
                "select table_name, column_name from information_schema.columns "
                "where table_name in ('refresh_tokens', 'platform_refresh_tokens') "
                "and column_name in ('last_seen_at', 'device_label') "
                "order by table_name, column_name"
            )
        ]
        nullable = await connection.fetchval(
            "select is_nullable from information_schema.columns "
            "where table_name = 'refresh_tokens' and column_name = 'last_seen_at'"
        )
        return tenant_reasons, platform_reasons, columns, str(nullable)
    finally:
        await connection.close()


async def _m1c2_backfill_mismatches(dsn: str) -> int:
    """升级后 last_seen_at 必须由 issued_at 回填（存量行不得为空或错值）。"""

    connection = await asyncpg.connect(dsn)
    try:
        tenant = await connection.fetchval(
            "select count(*) from refresh_tokens where last_seen_at is distinct from issued_at"
        )
        platform = await connection.fetchval(
            "select count(*) from platform_refresh_tokens "
            "where last_seen_at is distinct from issued_at"
        )
        return int(tenant) + int(platform)
    finally:
        await connection.close()


def test_m1f_downgrade_with_audit_rows_then_upgrade() -> None:
    """M1-F 复审 Minor-6：已有审计行时 downgrade 只摘触发器（数据保留），再 upgrade 后链继续。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)

    dsn, database = _admin_dsn(admin_url)
    temp_database = f"{database}_m1frollback"
    temp_dsn = dsn.rsplit("/", 1)[0] + f"/{temp_database}"
    temp_url = admin_url.rsplit("/", 1)[0] + f"/{temp_database}"

    async def seed_two_chained_rows() -> list[Record]:
        connection = await asyncpg.connect(temp_dsn)
        try:
            rows: list[Record] = []
            for action in ("test.m1f.rollback.1", "test.m1f.rollback.2"):
                async with connection.transaction():
                    await connection.execute(
                        "select set_config('app.audit_hash_key', $1, true)", M1F_TEST_KEY
                    )
                    rows.append(
                        dict(
                            await connection.fetchrow(
                                "insert into audit_logs "
                                "(event_id, actor_realm, action, resource_type, result, created_at) "
                                "values (gen_random_uuid(), 'system', $1, 'test', 'success', now()) "
                                "returning sequence, prev_hash, hash, hash_version",
                                action,
                            )
                        )
                    )
            return rows
        finally:
            await connection.close()

    async def rollback_state() -> tuple[bool, int]:
        connection = await asyncpg.connect(temp_dsn)
        try:
            trigger_gone = (
                await connection.fetchval(
                    "select count(*) from pg_trigger "
                    "where tgname = 'audit_logs_hash_chain' and not tgisinternal"
                )
            ) == 0
            chained_rows = await connection.fetchval(
                "select count(*) from audit_logs where hash is not null"
            )
            return trigger_gone, chained_rows
        finally:
            await connection.close()

    async def insert_after_reupgrade() -> Record:
        connection = await asyncpg.connect(temp_dsn)
        try:
            async with connection.transaction():
                await connection.execute(
                    "select set_config('app.audit_hash_key', $1, true)", M1F_TEST_KEY
                )
                return dict(
                    await connection.fetchrow(
                        "insert into audit_logs "
                        "(event_id, actor_realm, action, resource_type, result, created_at) "
                        "values (gen_random_uuid(), 'system', 'test.m1f.rollback.3', 'test', "
                        "'success', now()) returning sequence, prev_hash, hash, hash_version"
                    )
                )
        finally:
            await connection.close()

    asyncio.run(_create_database(dsn, temp_database))
    try:
        config = _alembic_config(temp_url)
        command.upgrade(config, "head")
        seeded = asyncio.run(seed_two_chained_rows())
        assert [row["hash_version"] for row in seeded] == [1, 1]
        assert seeded[1]["prev_hash"] == seeded[0]["hash"]

        command.downgrade(config, M1F_PREVIOUS_REVISION)
        trigger_gone, chained_rows = asyncio.run(rollback_state())
        assert trigger_gone, "downgrade 后哈希链触发器应被移除"
        assert chained_rows == 2, "downgrade 不得删除已有审计行（仅追加数据保留）"

        command.upgrade(config, "head")
        trigger_gone_after, _ = asyncio.run(rollback_state())
        assert not trigger_gone_after, "重新 upgrade 后触发器应恢复"
        fresh = asyncio.run(insert_after_reupgrade())
        assert fresh["hash_version"] == 1
        # 链在保留行之后继续：新行 prev_hash = 上一条已入链行的 hash
        assert fresh["prev_hash"] == seeded[-1]["hash"]
    finally:
        asyncio.run(_drop_database(dsn, temp_database))


def test_m2_org_downgrade_drops_tables_then_upgrade_restores() -> None:
    """M2-ORG01-03：downgrade 摘掉组织五表（数据随之消失，首点无存量语义），再 upgrade 后可重建。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)

    dsn, database = _admin_dsn(admin_url)
    temp_database = f"{database}_m2rollback"
    temp_dsn = dsn.rsplit("/", 1)[0] + f"/{temp_database}"
    temp_url = admin_url.rsplit("/", 1)[0] + f"/{temp_database}"

    async def table_exists(table: str) -> bool:
        connection = await asyncpg.connect(temp_dsn)
        try:
            value = await connection.fetchval(
                "select 1 from information_schema.tables "
                "where table_schema = 'public' and table_name = $1",
                table,
            )
            return value is not None
        finally:
            await connection.close()

    async def seed_class() -> UUID:
        connection = await asyncpg.connect(temp_dsn)
        try:
            tenant_id = await connection.fetchval(
                "insert into tenants (name, status) values ('M2 回滚中学', 'active') returning id"
            )
            await connection.execute(
                "insert into classes (tenant_id, entry_year, grade, name, status) "
                "values ($1, 2026, '2026级', '回滚班', 'active')",
                tenant_id,
            )
            return cast(UUID, tenant_id)
        finally:
            await connection.close()

    async def rebuild_class(tenant_id: UUID) -> None:
        connection = await asyncpg.connect(temp_dsn)
        try:
            await connection.execute(
                "insert into classes (tenant_id, entry_year, grade, name, status) "
                "values ($1, 2026, '2026级', '重建班', 'active')",
                tenant_id,
            )
        finally:
            await connection.close()

    asyncio.run(_create_database(dsn, temp_database))
    try:
        config = _alembic_config(temp_url)
        command.upgrade(config, "head")
        seeded_tenant = asyncio.run(seed_class())

        command.downgrade(config, M2_PREVIOUS_REVISION)
        for table in M2_ORG_TABLES:
            assert not asyncio.run(table_exists(table)), f"{table} 应随 downgrade 移除"
        # class 范围行部分唯一索引也应随 downgrade 移除（downgrade 里先 drop index）
        assert not asyncio.run(_m2_index_exists(temp_dsn, "uq_data_scopes_user_class"))

        command.upgrade(config, "head")
        asyncio.run(rebuild_class(seeded_tenant))
    finally:
        asyncio.run(_drop_database(dsn, temp_database))


async def _m2_index_exists(dsn: str, index_name: str) -> bool:
    connection = await asyncpg.connect(dsn)
    try:
        value = await connection.fetchval(
            "select 1 from pg_indexes where schemaname = 'public' and indexname = $1",
            index_name,
        )
        return value is not None
    finally:
        await connection.close()


def test_m3_kb_downgrade_drops_tables_then_upgrade_restores() -> None:
    """M3-KB-01A：downgrade 先解循环外键再删三表，重 upgrade 后可重建并再写入。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)

    dsn, database = _admin_dsn(admin_url)
    temp_database = f"{database}_m3kbrollback"
    temp_dsn = dsn.rsplit("/", 1)[0] + f"/{temp_database}"
    temp_url = admin_url.rsplit("/", 1)[0] + f"/{temp_database}"

    async def table_exists(table: str) -> bool:
        connection = await asyncpg.connect(temp_dsn)
        try:
            value = await connection.fetchval(
                "select 1 from information_schema.tables "
                "where table_schema = 'public' and table_name = $1",
                table,
            )
            return value is not None
        finally:
            await connection.close()

    async def seed_principal() -> tuple[UUID, UUID]:
        connection = await asyncpg.connect(temp_dsn)
        try:
            tenant_id = await connection.fetchval(
                "insert into tenants (name, status) values ('M3 回滚中学', 'active') returning id"
            )
            user_id = await connection.fetchval(
                "insert into users (tenant_id, user_type, login_id, password_hash, status, "
                "must_change_password, mfa_enabled) values "
                "($1, 'school_admin', 'kb-rollback', 'x', 'active', false, false) returning id",
                tenant_id,
            )
            return cast(UUID, tenant_id), cast(UUID, user_id)
        finally:
            await connection.close()

    async def seed_batch(tenant_id: UUID, user_id: UUID) -> None:
        connection = await asyncpg.connect(temp_dsn)
        try:
            await connection.execute(
                "insert into kb_upload_batches "
                "(tenant_id, created_by, idempotency_key, request_hash, file_count, total_bytes) "
                "values ($1, $2, 'rollback-key', repeat('0', 64), 1, 10)",
                tenant_id,
                user_id,
            )
        finally:
            await connection.close()

    asyncio.run(_create_database(dsn, temp_database))
    try:
        config = _alembic_config(temp_url)
        command.upgrade(config, "head")
        tenant_id, user_id = asyncio.run(seed_principal())
        asyncio.run(seed_batch(tenant_id, user_id))

        command.downgrade(config, M3_KB_PREVIOUS_REVISION)
        for table in M3_KB_TABLES:
            assert not asyncio.run(table_exists(table)), f"{table} 应随 downgrade 移除"

        command.upgrade(config, "head")
        for table in M3_KB_TABLES:
            assert asyncio.run(table_exists(table)), f"{table} 应随 upgrade 重建"
        asyncio.run(seed_batch(tenant_id, user_id))
    finally:
        asyncio.run(_drop_database(dsn, temp_database))


def test_m1c2_downgrade_with_session_reasons_then_upgrade() -> None:
    """含 rotated/session_* 行时可安全 downgrade（rotated 保留），再 upgrade。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)

    dsn, database = _admin_dsn(admin_url)
    temp_database = f"{database}_m1c2rollback"
    temp_dsn = dsn.rsplit("/", 1)[0] + f"/{temp_database}"
    temp_url = admin_url.rsplit("/", 1)[0] + f"/{temp_database}"

    asyncio.run(_create_database(dsn, temp_database))
    try:
        config = _alembic_config(temp_url)
        command.upgrade(config, "head")
        asyncio.run(_seed_m1c2_reasons(temp_dsn))

        command.downgrade(config, M1C2_PREVIOUS_REVISION)

        tenant_reasons, platform_reasons, columns, _nullable = asyncio.run(
            _m1c2_rollback_state(temp_dsn)
        )
        assert tenant_reasons == ["rotated", "logout", "logout", "logout"]
        assert platform_reasons == ["rotated", "logout", "logout", "logout"]
        assert columns == []

        command.upgrade(config, "head")
        _tenant_after, _platform_after, columns_after, nullable_after = asyncio.run(
            _m1c2_rollback_state(temp_dsn)
        )
        assert columns_after == [
            "platform_refresh_tokens.device_label",
            "platform_refresh_tokens.last_seen_at",
            "refresh_tokens.device_label",
            "refresh_tokens.last_seen_at",
        ]
        assert nullable_after == "NO"
        assert asyncio.run(_m1c2_backfill_mismatches(temp_dsn)) == 0
    finally:
        asyncio.run(_drop_database(dsn, temp_database))


async def _seed_m1d2_mfa_state(dsn: str) -> None:
    """写入启用 MFA 的用户、恢复码与 mfa_change 刷新令牌行。"""

    connection = await asyncpg.connect(dsn)
    try:
        tenant_id = await connection.fetchval(
            "insert into tenants (school_code, name, status, activated_at) "
            "values ('SCH-D2ROLL', 'D2 回滚中学', 'active', now()) returning id"
        )
        user_id = await connection.fetchval(
            "insert into users (tenant_id, user_type, login_id, password_hash, "
            "status, must_change_password, mfa_enabled, mfa_secret_enc, mfa_last_used_step) "
            "values ($1, 'student', 'd2-rollback@school.edu', '$2b$12$fake', "
            "'active', false, true, 'v1.nonce.ciphertext', 123) returning id",
            tenant_id,
        )
        await connection.execute(
            "insert into refresh_tokens (tenant_id, user_id, jti, family_id, "
            "token_hash, expires_at, last_seen_at, revoked_at, revoked_reason, mfa_verified) "
            "values ($1, $2, gen_random_uuid(), gen_random_uuid(), $3, "
            "now() + interval '1 day', now(), now(), 'mfa_change', true)",
            tenant_id,
            user_id,
            "f" * 64,
        )
        await connection.execute(
            "insert into mfa_recovery_codes (tenant_id, user_id, code_hash) values ($1, $2, $3)",
            tenant_id,
            user_id,
            "a" * 64,
        )

        platform_user_id = await connection.fetchval(
            "insert into platform_users (account, password_hash, status, "
            "must_change_password, mfa_enabled, mfa_secret_enc, mfa_last_used_step) "
            "values ('d2-rollback-ops', '$2b$12$fake', 'active', false, true, "
            "'v1.nonce.ciphertext', 456) returning id"
        )
        await connection.execute(
            "insert into platform_refresh_tokens (platform_user_id, jti, family_id, "
            "token_hash, expires_at, last_seen_at, revoked_at, revoked_reason, mfa_verified) "
            "values ($1, gen_random_uuid(), gen_random_uuid(), $2, "
            "now() + interval '1 day', now(), now(), 'mfa_change', true)",
            platform_user_id,
            "b" * 64,
        )
        await connection.execute(
            "insert into platform_mfa_recovery_codes (platform_user_id, code_hash) values ($1, $2)",
            platform_user_id,
            "c" * 64,
        )
    finally:
        await connection.close()


async def _d2_rollback_state(dsn: str) -> tuple[list[str], bool, bool]:
    """返回回滚后的撤销原因、MFA 表是否删除、MFA 列是否删除。"""

    connection = await asyncpg.connect(dsn)
    try:
        tenant_reasons = [
            str(row["revoked_reason"])
            for row in await connection.fetch("select revoked_reason from refresh_tokens")
        ]
        platform_reasons = [
            str(row["revoked_reason"])
            for row in await connection.fetch("select revoked_reason from platform_refresh_tokens")
        ]
        tables_gone = await connection.fetchval(
            "select to_regclass('mfa_recovery_codes') is null "
            "and to_regclass('platform_mfa_recovery_codes') is null"
        )
        columns_gone = await connection.fetchval(
            "select count(*) = 0 from information_schema.columns "
            "where column_name in ('mfa_last_used_step', 'mfa_verified') "
            "and table_name in ('users', 'platform_users', 'refresh_tokens', "
            "'platform_refresh_tokens')"
        )
        return tenant_reasons + platform_reasons, bool(tables_gone), bool(columns_gone)
    finally:
        await connection.close()


def test_m1d2_downgrade_normalizes_mfa_change_reason_then_upgrade() -> None:
    """含 mfa_change 撤销原因时可 downgrade（归一化为 admin_revoke），再 upgrade。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)

    dsn, database = _admin_dsn(admin_url)
    temp_database = f"{database}_m1d2rollback"
    temp_dsn = dsn.rsplit("/", 1)[0] + f"/{temp_database}"
    temp_url = admin_url.rsplit("/", 1)[0] + f"/{temp_database}"

    asyncio.run(_create_database(dsn, temp_database))
    try:
        config = _alembic_config(temp_url)
        command.upgrade(config, "head")
        asyncio.run(_seed_m1d2_mfa_state(temp_dsn))

        command.downgrade(config, D1_REVISION)

        reasons, tables_gone, columns_gone = asyncio.run(_d2_rollback_state(temp_dsn))
        assert reasons == ["admin_revoke", "admin_revoke"]
        assert tables_gone is True
        assert columns_gone is True

        command.upgrade(config, "head")
        assert asyncio.run(_table_exists(temp_dsn, "mfa_recovery_codes")) is True
        assert asyncio.run(_table_exists(temp_dsn, "platform_mfa_recovery_codes")) is True
    finally:
        asyncio.run(_drop_database(dsn, temp_database))


def test_m1d2_upgrade_fails_fast_on_enabled_mfa_without_secret() -> None:
    """存量行 mfa_enabled=true 但 secret 为空时，升级必须失败并提示先修复数据。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)

    dsn, database = _admin_dsn(admin_url)
    temp_database = f"{database}_m1d2precheck"
    temp_dsn = dsn.rsplit("/", 1)[0] + f"/{temp_database}"
    temp_url = admin_url.rsplit("/", 1)[0] + f"/{temp_database}"

    asyncio.run(_create_database(dsn, temp_database))
    try:
        config = _alembic_config(temp_url)
        command.upgrade(config, D1_REVISION)
        asyncio.run(_seed_invalid_mfa_row(temp_dsn))

        with pytest.raises(RuntimeError, match="mfa_secret_enc"):
            command.upgrade(config, "head")
    finally:
        asyncio.run(_drop_database(dsn, temp_database))


async def _seed_invalid_mfa_row(dsn: str) -> None:
    """在 D1 schema 下写入 mfa_enabled=true 且无 secret 的平台账号。"""

    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(
            "insert into platform_users (account, password_hash, status, "
            "must_change_password, mfa_enabled) "
            "values ('d2-invalid-ops', '$2b$12$fake', 'active', false, true)"
        )
    finally:
        await connection.close()


async def _seed_d1_password_reset_reason(dsn: str) -> None:
    """写入 revoked_reason='password_reset' 的租户/平台刷新令牌行。"""

    connection = await asyncpg.connect(dsn)
    try:
        tenant_id = await connection.fetchval(
            "insert into tenants (school_code, name, status, activated_at) "
            "values ('SCH-D1ROLL', 'D1 回滚中学', 'active', now()) returning id"
        )
        user_id = await connection.fetchval(
            "insert into users (tenant_id, user_type, login_id, password_hash, "
            "status, must_change_password, mfa_enabled) "
            "values ($1, 'student', 'd1-rollback@school.edu', '$2b$12$fake', "
            "'active', false, false) returning id",
            tenant_id,
        )
        await connection.execute(
            "insert into refresh_tokens (tenant_id, user_id, jti, family_id, "
            "token_hash, expires_at, last_seen_at, revoked_at, revoked_reason) "
            "values ($1, $2, gen_random_uuid(), gen_random_uuid(), $3, "
            "now() + interval '1 day', now(), now(), 'password_reset')",
            tenant_id,
            user_id,
            "d" * 64,
        )
        platform_user_id = await connection.fetchval(
            "insert into platform_users (account, password_hash, status, "
            "must_change_password, mfa_enabled) "
            "values ('d1-rollback-ops', '$2b$12$fake', 'active', false, false) "
            "returning id"
        )
        await connection.execute(
            "insert into platform_refresh_tokens (platform_user_id, jti, family_id, "
            "token_hash, expires_at, last_seen_at, revoked_at, revoked_reason) "
            "values ($1, gen_random_uuid(), gen_random_uuid(), $2, "
            "now() + interval '1 day', now(), now(), 'password_reset')",
            platform_user_id,
            "e" * 64,
        )
    finally:
        await connection.close()


async def _d1_rollback_state(dsn: str) -> tuple[list[str], list[str], bool]:
    """返回回滚后的撤销原因（应归一化为 password_change）与 D1 表是否已删除。"""

    connection = await asyncpg.connect(dsn)
    try:
        tenant_reasons = [
            str(row["revoked_reason"])
            for row in await connection.fetch("select revoked_reason from refresh_tokens")
        ]
        platform_reasons = [
            str(row["revoked_reason"])
            for row in await connection.fetch("select revoked_reason from platform_refresh_tokens")
        ]
        table_gone = await connection.fetchval(
            "select to_regclass('password_reset_tokens') is null "
            "and to_regclass('platform_password_reset_tokens') is null "
            "and to_regclass('password_history') is null "
            "and to_regclass('platform_password_history') is null"
        )
        return tenant_reasons, platform_reasons, bool(table_gone)
    finally:
        await connection.close()


async def _table_exists(dsn: str, table: str) -> bool:
    """表是否存在（升级后复核 D1 表重建）。"""

    connection = await asyncpg.connect(dsn)
    try:
        return bool(await connection.fetchval(f"select to_regclass('{table}') is not null"))
    finally:
        await connection.close()


def test_m1d1_downgrade_normalizes_password_reset_reason_then_upgrade() -> None:
    """含 password_reset 撤销原因时可 downgrade（先归一化为 password_change），再 upgrade。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)

    dsn, database = _admin_dsn(admin_url)
    temp_database = f"{database}_m1d1rollback"
    temp_dsn = dsn.rsplit("/", 1)[0] + f"/{temp_database}"
    temp_url = admin_url.rsplit("/", 1)[0] + f"/{temp_database}"

    asyncio.run(_create_database(dsn, temp_database))
    try:
        config = _alembic_config(temp_url)
        command.upgrade(config, "head")
        asyncio.run(_seed_d1_password_reset_reason(temp_dsn))

        command.downgrade(config, D1_PREVIOUS_REVISION)

        tenant_reasons, platform_reasons, table_gone = asyncio.run(_d1_rollback_state(temp_dsn))
        assert tenant_reasons == ["password_change"]
        assert platform_reasons == ["password_change"]
        assert table_gone is True

        command.upgrade(config, "head")
        assert asyncio.run(_table_exists(temp_dsn, "password_reset_tokens")) is True
    finally:
        asyncio.run(_drop_database(dsn, temp_database))
