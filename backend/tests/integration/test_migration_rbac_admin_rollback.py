"""M2-UI2 迁移往返回归（独立临时库）：升级→清空→降级→再升级。

钉住 TD-150 先决迁移最容易写坏的五件事：

1. 升级把存量角色的 `defaults_applied` 回填为 `true`，且**不把升级前刚被清空的映射补回来**
   （否则“撤销一条=白撤”的 bug 会在升级瞬间复现）；
2. `school.personnel.mfa.reset` 落库并只落到全量型超管（TD-126 的默认授权口径）；
3. 升级后的 `seed_builtin_roles(uuid)` 按标记补一次，新开通租户当场 `defaults_applied=true`；
4. 降级删除三列与新增权限码，并把开放函数退回 E2 原文；
5. 再升级仍可重放：被清空的角色保持清空，超管重新拿到新码。
"""

import asyncio
import json
import os
from uuid import uuid4

import asyncpg
import pytest

from tests.support.dsn import assert_test_database_dsn
from tests.support.migrations import (
    alembic_config,
    column_exists,
    create_database,
    downgrade,
    drop_database,
    fetch,
    function_defines,
    scalar,
    temp_database_url,
    upgrade,
)

pytestmark = pytest.mark.integration

M2_REVISION = "c3f7a9d1e5b2"
PREV_REVISION = "b9d3f7c1e5a9"
MFA_CODE = "school.personnel.mfa.reset"


async def _execute(dsn: str, sql: str, *args: object) -> None:
    """临时库上执行写语句（迁移往返用例里模拟运营清空）。"""

    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(sql, *args)
    finally:
        await connection.close()


async def _provision_tenant(dsn: str) -> str:
    """走真正的开通函数建租户（覆盖开通链路上的 seed 语义）。"""

    payload = {
        "school_code": f"UI2RT{uuid4().hex[:6].upper()}",
        "name": "角色后台往返测试学校",
        "address": "测试路 3 号",
        "email": "rbac-roundtrip@school.edu",
        "leadership": "校长",
        "qualification_ref": "private/qualification/rbac-roundtrip.pdf",
        "admin_id": str(uuid4()),
        "admin_login": "rbac-roundtrip@school.edu",
        "admin_display_name": "角色后台往返测试管理员",
        "admin_password_hash": "$2b$12$roundtriptesthash",
    }
    value = await scalar(dsn, "select public.provision_tenant($1::jsonb)", json.dumps(payload))
    return str(value)


async def _role_permissions(dsn: str, tenant_id: str, role_code: str) -> set[str]:
    rows = await fetch(
        dsn,
        """
        select p.code
        from public.roles r
        join public.role_permissions rp on rp.role_id = r.id
        join public.permissions p on p.id = rp.permission_id
        where r.tenant_id = $1 and r.code = $2
        """,
        tenant_id,
        role_code,
    )
    return {str(row["code"]) for row in rows}


async def _role_markers(dsn: str, tenant_id: str) -> dict[str, bool]:
    rows = await fetch(
        dsn,
        "select code, defaults_applied from public.roles where tenant_id = $1",
        tenant_id,
    )
    return {str(row["code"]): bool(row["defaults_applied"]) for row in rows}


async def _scope_types(dsn: str, tenant_id: str, role_code: str) -> set[str]:
    rows = await fetch(
        dsn,
        """
        select s.scope_type
        from public.roles r
        join public.data_scopes s on s.role_id = r.id and s.tenant_id = r.tenant_id
        where r.tenant_id = $1 and r.code = $2
        """,
        tenant_id,
        role_code,
    )
    return {str(row["scope_type"]) for row in rows}


def test_m2_ui2_migration_round_trip_defaults_marker_and_mfa_code() -> None:
    """标记回填不复活清空项→降级删列删码→再升级可重放。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)

    admin_dsn, temp_url, temp_database = temp_database_url(admin_url, "m2ui2_roundtrip")
    asyncio.run(create_database(admin_dsn, temp_database))
    config = alembic_config(temp_url)
    try:
        # ---------- 先到 M2-UI2 之前，开通一个租户并模拟运营“清空” ----------
        upgrade(config, PREV_REVISION)
        dsn = temp_url.replace("postgresql+asyncpg://", "postgresql://")
        tenant_id = asyncio.run(_provision_tenant(dsn))

        assert asyncio.run(_role_permissions(dsn, tenant_id, "school_admin"))
        assert asyncio.run(_scope_types(dsn, tenant_id, "teacher_b_end")) == {"self"}

        # 运营在旧界面上把管理员的权限与教师 B 端的范围清空
        asyncio.run(
            _execute(
                dsn,
                """
                delete from public.role_permissions
                where role_id in (
                    select id from public.roles
                    where tenant_id = $1 and code = 'school_admin'
                )
                """,
                tenant_id,
            )
        )
        asyncio.run(
            _execute(
                dsn,
                """
                delete from public.data_scopes
                where role_id in (
                    select id from public.roles
                    where tenant_id = $1 and code = 'teacher_b_end'
                )
                """,
                tenant_id,
            )
        )

        # ---------- 升级到 head（含 M2-UI2） ----------
        upgrade(config)

        assert asyncio.run(column_exists(dsn, "roles", "defaults_applied"))
        assert asyncio.run(column_exists(dsn, "roles", "version"))
        assert asyncio.run(column_exists(dsn, "platform_roles", "defaults_applied"))
        assert asyncio.run(_role_markers(dsn, tenant_id)) == {
            "school_super_admin": True,
            "school_admin": True,
            "teacher_b_end": True,
            "student": True,
            "teacher": True,
        }
        # 升级回填 true ⇒ 同步不会把升级前的清空补回来（TD-150 的核心断言）
        assert asyncio.run(_role_permissions(dsn, tenant_id, "school_admin")) == set()
        assert asyncio.run(_scope_types(dsn, tenant_id, "teacher_b_end")) == set()
        # 全量型超管按“只补不撤”拿到新码，其它角色不默认给
        assert MFA_CODE in asyncio.run(
            _role_permissions(dsn, tenant_id, "school_super_admin")
        )
        assert MFA_CODE not in asyncio.run(_role_permissions(dsn, tenant_id, "school_admin"))
        seed_body = asyncio.run(function_defines(dsn, "seed_builtin_roles"))
        assert "defaults_applied" in seed_body

        # ---------- 升级后新开通的租户当场初始化完成 ----------
        fresh_tenant = asyncio.run(_provision_tenant(dsn))
        assert asyncio.run(_role_markers(dsn, fresh_tenant)) == {
            "school_super_admin": True,
            "school_admin": True,
            "teacher_b_end": True,
            "student": True,
            "teacher": True,
        }
        assert MFA_CODE in asyncio.run(_role_permissions(dsn, fresh_tenant, "school_super_admin"))
        assert MFA_CODE not in asyncio.run(_role_permissions(dsn, fresh_tenant, "school_admin"))
        assert asyncio.run(_scope_types(dsn, fresh_tenant, "teacher_b_end")) == {"self"}

        # ---------- 降级回 M2-UI2 之前 ----------
        downgrade(config, PREV_REVISION)

        assert not asyncio.run(column_exists(dsn, "roles", "defaults_applied"))
        assert not asyncio.run(column_exists(dsn, "roles", "version"))
        assert not asyncio.run(column_exists(dsn, "platform_roles", "defaults_applied"))
        legacy_seed = asyncio.run(function_defines(dsn, "seed_builtin_roles"))
        assert "defaults_applied" not in legacy_seed
        assert asyncio.run(
            scalar(dsn, "select count(*) from public.permissions where code = $1", MFA_CODE)
        ) == 0

        # ---------- 再升级：仍可重放，清空保持、超管重新拿到新码 ----------
        upgrade(config)
        assert asyncio.run(_role_markers(dsn, tenant_id)) == {
            "school_super_admin": True,
            "school_admin": True,
            "teacher_b_end": True,
            "student": True,
            "teacher": True,
        }
        assert asyncio.run(_role_permissions(dsn, tenant_id, "school_admin")) == set()
        assert MFA_CODE in asyncio.run(
            _role_permissions(dsn, tenant_id, "school_super_admin")
        )
    finally:
        asyncio.run(drop_database(admin_dsn, temp_database))
