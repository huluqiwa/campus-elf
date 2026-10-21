"""M1-B 迁移回滚回归：存在校方超管数据时可安全 downgrade / 再 upgrade。

审查发现：旧版 downgrade 直接恢复 `user_type` 旧约束，遇到 school_admin 行会失败。
本测试在**独立临时数据库**中复现"先升级、再写入超管、再降级"，避免污染主测试库。
"""

import asyncio
import os
import re
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from tests.support.db import BACKEND_ROOT
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.integration]

M1A_REVISION = "779f731db342"
M1C2_PREVIOUS_REVISION = "c8e2f4a6b1d3"


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
            await connection.fetchval(
                "select count(*) from users where user_type = 'school_admin'"
            )
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
            "DROP TRIGGER IF EXISTS school_review_records_no_update_delete "
            "ON school_review_records"
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
        await connection.execute(
            "GRANT INSERT ON school_review_records TO campus_elf_platform"
        )
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
        assert provision_acl is not None and not public_grant.search(provision_acl), (
            provision_acl
        )
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
    temp_platform_dsn = platform_url.replace("postgresql+asyncpg://", "postgresql://").rsplit(
        "/", 1
    )[0] + f"/{temp_database}"

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
    saved = {
        key: os.environ.get(key)
        for key in ("DATABASE_URL", "PLATFORM_DATABASE_URL")
    }
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
        for index, reason in enumerate(
            ("rotated", "session_kicked", "session_revoked", "logout")
        ):
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
        for index, reason in enumerate(
            ("rotated", "session_kicked", "session_revoked", "logout")
        ):
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
            "select count(*) from refresh_tokens "
            "where last_seen_at is distinct from issued_at"
        )
        platform = await connection.fetchval(
            "select count(*) from platform_refresh_tokens "
            "where last_seen_at is distinct from issued_at"
        )
        return int(tenant) + int(platform)
    finally:
        await connection.close()


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
