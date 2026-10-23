"""M1-E2 迁移往返回归（独立临时库）：升级→开通→降级→再升级。

E2 的 downgrade 会把函数体退回 E1 原文、删掉两张新表与三列一约束，并撤掉按出厂默认
写入的 `data_scopes` 行 —— 这些语句在 CI 里只跑过 upgrade，没跑过就等于没写过。
本用例钉住往返的六件事：

1. 升级后两张定义/设置表在库、列与 `CHECK` 就位（RBAC-03 的硬闸门不是纸面口径）；
2. `role_permissions_realm_guard()` 确实是白名单版（TD-149 收紧生效，不是只改了注释）；
3. **真正的开通函数**能给新租户种出按矩阵 §2 的出厂默认数据范围（超管/管理员=全校，其余=本人）；
4. 授权面：`system_settings` 只有平台角色读得到，租户角色连 SELECT 都没有；
5. 降级把以上全部退回 E1 状态（函数、表、列、约束、范围行、授权一个不留）；
6. 再升级仍可重放，且存量租户的范围按「只补缺」重新长回来。
"""

import asyncio
import json
import os
from typing import Any
from uuid import uuid4

import pytest

from app.rbac.catalog import TENANT_MODULES
from app.rbac.seeds import TENANT_BUILTIN_ROLES
from tests.support.dsn import assert_test_database_dsn
from tests.support.migrations import (
    alembic_config,
    column_exists,
    constraint_exists,
    create_database,
    downgrade,
    drop_database,
    fetch,
    function_defines,
    role_can_select,
    scalar,
    table_exists,
    temp_database_url,
    upgrade,
)

pytestmark = pytest.mark.integration

E1_REVISION = "e5f7a9c1b3d5"
SCOPE_DEFAULTS_TABLE = "rbac_builtin_role_scope_defaults"
SYSTEM_SETTINGS_TABLE = "system_settings"
# 库里的实际约束名：`op.create_check_constraint(name=...)` 会过 `app/db/base.py` 的命名约定
# `ck_%(table_name)s_%(constraint_name)s`，所以带表名前缀（downgrade 的 drop 必须用同一个名字）。
SELF_APPROVAL_CHECK = "ck_school_review_records_no_self_approval_without_exception"


def _expected_builtin_scopes() -> dict[str, set[str]]:
    """出厂默认数据范围的期望值（唯一来源 `seeds.py`，不在测试里抄第二份字面量）。"""

    return {role.code: set(role.scopes) for role in TENANT_BUILTIN_ROLES if role.realm == "tenant"}


async def _role_scopes(dsn: str, tenant_id: str) -> dict[str, set[str]]:
    rows = await fetch(
        dsn,
        """
        select r.code, s.scope_type
        from public.roles r
        join public.data_scopes s on s.role_id = r.id and s.tenant_id = r.tenant_id
        where r.tenant_id = $1
        """,
        tenant_id,
    )
    grouped: dict[str, set[str]] = {}
    for row in rows:
        grouped.setdefault(str(row["code"]), set()).add(str(row["scope_type"]))
    return grouped


async def _scope_row_count(dsn: str, tenant_id: str) -> int:
    value = await scalar(
        dsn, "select count(*) from public.data_scopes where tenant_id = $1", tenant_id
    )
    return int(value)


async def _provision_tenant(dsn: str) -> str:
    """走真正的开通函数建租户（本用例要验证的是函数里的范围补齐，不是手搓 INSERT）。"""

    payload: dict[str, Any] = {
        "school_code": f"E2RT{uuid4().hex[:6].upper()}",
        "name": "范围往返测试学校",
        "address": "测试路 2 号",
        "email": "scope-roundtrip@school.edu",
        "leadership": "校长",
        "qualification_ref": "private/qualification/roundtrip.pdf",
        "admin_id": str(uuid4()),
        "admin_login": "scope-roundtrip@school.edu",
        "admin_display_name": "范围往返测试管理员",
        "admin_password_hash": "$2b$12$roundtriptesthash",
    }
    value = await scalar(dsn, "select public.provision_tenant($1::jsonb)", json.dumps(payload))
    return str(value)


def test_e2_migration_round_trip_data_scope_and_self_approval_gate() -> None:
    """升级长出范围默认与自批闸门→降级逐件退回→再升级可重放。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)

    admin_dsn, temp_url, temp_database = temp_database_url(admin_url, "e2_roundtrip")
    asyncio.run(create_database(admin_dsn, temp_database))
    config = alembic_config(temp_url)
    try:
        # ---------- 升级到 head（含 E2） ----------
        upgrade(config)
        dsn = temp_url.replace("postgresql+asyncpg://", "postgresql://")

        assert asyncio.run(table_exists(dsn, SCOPE_DEFAULTS_TABLE))
        assert asyncio.run(table_exists(dsn, SYSTEM_SETTINGS_TABLE))
        assert asyncio.run(
            column_exists(dsn, "tenant_registration_requests", "submitted_by_platform_user_id")
        )
        assert asyncio.run(
            column_exists(dsn, "school_review_records", "submitted_by_platform_user_id")
        )
        assert asyncio.run(column_exists(dsn, "school_review_records", "self_exception_used"))
        assert asyncio.run(constraint_exists(dsn, SELF_APPROVAL_CHECK))

        # realm 触发器：白名单版（口径来自 catalog.TENANT_MODULES，两侧同源）
        guard_body = asyncio.run(function_defines(dsn, "role_permissions_realm_guard"))
        assert "NOT IN" in guard_body, "realm 守卫未收紧成白名单"
        for module in sorted(TENANT_MODULES):
            assert f"'{module}'" in guard_body

        # 审核函数：RBAC-03 的第二道锁真的在函数里
        review_body = asyncio.run(function_defines(dsn, "review_registration_request"))
        assert "SELF_APPROVAL_FORBIDDEN" in review_body
        assert "SELF_APPROVAL_REASON_REQUIRED" in review_body
        assert SYSTEM_SETTINGS_TABLE in review_body
        assert "self_exception_used" in review_body

        # 授权面：设置表只有平台角色可读写，租户角色连读都不行
        assert asyncio.run(role_can_select(dsn, "campus_elf_platform", SYSTEM_SETTINGS_TABLE))
        assert not asyncio.run(
            role_can_select(dsn, "campus_elf_tenant", SYSTEM_SETTINGS_TABLE)
        ), "租户角色能读 system_settings"

        # ---------- 开通函数种出出厂默认数据范围 ----------
        expected = _expected_builtin_scopes()
        tenant_id = asyncio.run(_provision_tenant(dsn))
        assert asyncio.run(_role_scopes(dsn, tenant_id)) == expected
        assert expected["school_super_admin"] == {"school"}
        assert expected["teacher"] == {"self"}

        # ---------- 降级回 E2 之前 ----------
        downgrade(config, E1_REVISION)

        assert not asyncio.run(table_exists(dsn, SCOPE_DEFAULTS_TABLE))
        assert not asyncio.run(table_exists(dsn, SYSTEM_SETTINGS_TABLE))
        assert not asyncio.run(
            column_exists(dsn, "tenant_registration_requests", "submitted_by_platform_user_id")
        )
        assert not asyncio.run(column_exists(dsn, "school_review_records", "self_exception_used"))
        assert not asyncio.run(constraint_exists(dsn, SELF_APPROVAL_CHECK))

        legacy_guard = asyncio.run(function_defines(dsn, "role_permissions_realm_guard"))
        assert "NOT IN" not in legacy_guard, "realm 守卫没退回收紧前的黑名单口径"
        assert "v_module = 'platform'" in legacy_guard

        legacy_review = asyncio.run(function_defines(dsn, "review_registration_request"))
        assert "SELF_APPROVAL_FORBIDDEN" not in legacy_review
        seed_body = asyncio.run(function_defines(dsn, "seed_builtin_roles"))
        assert SCOPE_DEFAULTS_TABLE not in seed_body, "种子函数还在写数据范围默认"

        # 按出厂默认写入的范围行随降级撤走（TD-138 的原状态：库里没有范围配置）
        assert asyncio.run(_scope_row_count(dsn, tenant_id)) == 0

        # ---------- 再升级：往返后仍可重放，存量租户的默认范围补回来 ----------
        upgrade(config)
        assert asyncio.run(_role_scopes(dsn, tenant_id)) == expected
        replayed_tenant = asyncio.run(_provision_tenant(dsn))
        assert asyncio.run(_role_scopes(dsn, replayed_tenant)) == expected
    finally:
        asyncio.run(drop_database(admin_dsn, temp_database))
