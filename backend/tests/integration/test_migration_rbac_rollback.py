"""M1-E1 迁移往返回归（独立临时库）：升级→开通→降级→再升级。

钉住 E1 迁移最容易被写坏的四件事：

1. 升级后目录与代码一致（9 个重复旧码消失、59 个矩阵码在库）；
2. **新开通路径真的能种出内置角色**：`provision_tenant()` 改走 `seed_builtin_roles()`，
   所以必须从函数入口跑一遍，断言库里的角色与权限，而不是只断言定义表有数据；
3. 降级回到 E1 之前的口径：两张定义表与种子函数消失、目录回到旧 20 码、
   `student`/`teacher` 角色消失、超管映射退回「旧目录的 school+campus」、
   两个开通函数体恢复原文（不再调用 seed_builtin_roles）；
4. 再升级仍然成功（往返不留下无法重放的状态）。
"""

import asyncio
import json
import os
from uuid import uuid4

import pytest

from app.db.models.rbac import PERMISSION_CATALOG as LEGACY_PERMISSION_CATALOG
from app.rbac.catalog import catalog_codes, codes_with_module, legacy_codes_removed_by_m1e1
from tests.support.dsn import assert_test_database_dsn
from tests.support.migrations import (
    alembic_config,
    create_database,
    downgrade,
    drop_database,
    fetch,
    function_defines,
    scalar,
    table_exists,
    temp_database_url,
    upgrade,
)

pytestmark = pytest.mark.integration

D2_REVISION = "c4d6e8f0a2b3"

# 旧目录里校方/C 端模块的码（M1-A 的 provision 给超管的就是这批）
_LEGACY_TENANT_MODULE_CODES: frozenset[str] = frozenset(
    code for code, module, _ in LEGACY_PERMISSION_CATALOG if module in ("school", "campus")
)
_LEGACY_CODES: frozenset[str] = frozenset(code for code, _, _ in LEGACY_PERMISSION_CATALOG)


async def _permission_codes(dsn: str) -> set[str]:
    rows = await fetch(dsn, "select code from public.permissions")
    return {row["code"] for row in rows}


async def _tenant_role_codes(dsn: str, tenant_id: str) -> set[str]:
    rows = await fetch(dsn, "select code from public.roles where tenant_id = $1", tenant_id)
    return {row["code"] for row in rows}


async def _role_permission_codes(dsn: str, tenant_id: str, role_code: str) -> set[str]:
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
    return {row["code"] for row in rows}


async def _provision_tenant(dsn: str) -> str:
    """走真正的开通函数建租户（不手搓 INSERT：本用例要验证的就是这条路径）。"""

    payload = {
        "school_code": f"E1RT{uuid4().hex[:6].upper()}",
        "name": "往返测试学校",
        "address": "测试路 1 号",
        "email": "roundtrip@school.edu",
        "leadership": "校长",
        "qualification_ref": "private/qualification/roundtrip.pdf",
        "admin_id": str(uuid4()),
        "admin_login": "roundtrip@school.edu",
        "admin_display_name": "往返测试管理员",
        "admin_password_hash": "$2b$12$roundtriptesthash",
    }
    tenant_id = await scalar(dsn, "select public.provision_tenant($1::jsonb)", json.dumps(payload))
    return str(tenant_id)


def test_e1_migration_round_trip_with_provisioned_tenant() -> None:
    """升级→provision 出五角色→降级退回三角色与旧目录→再升级仍可用。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)

    admin_dsn, temp_url, temp_database = temp_database_url(admin_url, "e1_roundtrip")
    asyncio.run(create_database(admin_dsn, temp_database))
    config = alembic_config(temp_url)
    try:
        # ---------- 升级到 head（含 E1） ----------
        upgrade(config)
        dsn = temp_url.replace("postgresql+asyncpg://", "postgresql://")

        assert asyncio.run(_permission_codes(dsn)) == set(catalog_codes())
        assert not (
            asyncio.run(_permission_codes(dsn)) & set(legacy_codes_removed_by_m1e1())
        ), "与矩阵重复的旧码未删除"
        assert asyncio.run(table_exists(dsn, "rbac_builtin_roles"))
        assert asyncio.run(table_exists(dsn, "rbac_builtin_role_defaults"))
        assert "seed_builtin_roles" in asyncio.run(function_defines(dsn, "provision_tenant"))
        assert "seed_builtin_roles" in asyncio.run(
            function_defines(dsn, "review_registration_request")
        )

        # ---------- 开通函数必须能种出内置角色与出厂默认 ----------
        tenant_id = asyncio.run(_provision_tenant(dsn))
        assert asyncio.run(_tenant_role_codes(dsn, tenant_id)) == {
            "school_super_admin",
            "school_admin",
            "teacher_b_end",
            "student",
            "teacher",
        }
        assert asyncio.run(_role_permission_codes(dsn, tenant_id, "school_super_admin")) == (
            codes_with_module("school") | codes_with_module("campus")
        )
        assert asyncio.run(_role_permission_codes(dsn, tenant_id, "school_admin")) == {
            "school.dashboard.view",
            "school.personnel.view",
            "school.kb.view",
            "school.message.view",
            "school.message.send",
            "school.message.read",
        }
        assert asyncio.run(_role_permission_codes(dsn, tenant_id, "teacher_b_end")) == {
            "school.kb.upload"
        }
        student_perms = asyncio.run(_role_permission_codes(dsn, tenant_id, "student"))
        assert "campus.chat.use" in student_perms and "campus.leave.create" in student_perms
        assert "campus.leave.create" not in asyncio.run(
            _role_permission_codes(dsn, tenant_id, "teacher")
        )
        assert "platform.tenant.review" not in asyncio.run(
            _role_permission_codes(dsn, tenant_id, "school_super_admin")
        )

        # ---------- 降级回 E1 之前 ----------
        downgrade(config, D2_REVISION)

        assert not asyncio.run(table_exists(dsn, "rbac_builtin_roles"))
        assert not asyncio.run(table_exists(dsn, "rbac_builtin_role_defaults"))
        assert asyncio.run(_permission_codes(dsn)) == set(_LEGACY_CODES)
        assert asyncio.run(_tenant_role_codes(dsn, tenant_id)) == {
            "school_super_admin",
            "school_admin",
            "teacher_b_end",
        }
        assert asyncio.run(_role_permission_codes(dsn, tenant_id, "school_super_admin")) == (
            _LEGACY_TENANT_MODULE_CODES
        )
        assert asyncio.run(_role_permission_codes(dsn, tenant_id, "school_admin")) == set()
        assert asyncio.run(_role_permission_codes(dsn, tenant_id, "teacher_b_end")) == set()
        assert "seed_builtin_roles" not in asyncio.run(function_defines(dsn, "provision_tenant"))
        assert "seed_builtin_roles" not in asyncio.run(
            function_defines(dsn, "review_registration_request")
        )
        # 旧目录里没有 platform.message.* / observability.* 等码：平台侧映射也必须已退回
        assert asyncio.run(
            scalar(dsn, "select count(*) from public.permissions where module = 'eval'")
        ) == 0

        # ---------- 再升级：往返后仍可重放 ----------
        upgrade(config)
        assert asyncio.run(_permission_codes(dsn)) == set(catalog_codes())
        replayed_tenant = asyncio.run(_provision_tenant(dsn))
        assert asyncio.run(_tenant_role_codes(dsn, replayed_tenant)) == {
            "school_super_admin",
            "school_admin",
            "teacher_b_end",
            "student",
            "teacher",
        }
        # 降级前那个租户的超管按新目录重新对齐（全量型角色口径）
        assert asyncio.run(_role_permission_codes(dsn, tenant_id, "school_super_admin")) == (
            codes_with_module("school") | codes_with_module("campus")
        )
    finally:
        asyncio.run(drop_database(admin_dsn, temp_database))
