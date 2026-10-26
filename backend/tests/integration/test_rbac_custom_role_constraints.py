"""自定义角色的库层约束与目录落库（M2-UI3 TD-187a）。

钉住三件事（简报 §4.1 + §11 决策 #6）：

1. 新权限码 `school.role.delete` 随代码目录同步落库，且校方超管按「本校全量」补到——
   目录加了码但超管没补上 = 新能力只有手写 SQL 才能授出；
2. `roles.code` 格式约束：只有 `^[a-z][a-z0-9_]{2,62}$` 进得了库。接口层的正则会随重构漂移，
   库层这条是「绕过应用代码的手工 SQL」的最后防线；
3. 内置角色码保留：非系统角色不得占用内置 code。靠「本租户恰好已有同码内置行」的唯一约束
   不够——那是逐租户偶然成立，跨租户一致性必须由 CHECK 声明。

每条都配反证（合法值必须能插入），否则约束可能是空挡或写过头。
"""

import os
from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.rbac import sync_rbac_definitions
from app.rbac.admin import EDITABLE_ROLE_CODES
from app.rbac.seeds import TENANT_BUILTIN_ROLES
from app.rbac.self_grant import SENSITIVE_SELF_GRANT_CODES
from tests.support.db import (
    EngineRole,
    clean_test_data,
    make_session_factory,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn

pytestmark = pytest.mark.integration

_ADMIN_ROLE: EngineRole = "admin"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    """确保测试库结构为最新（迁移失败即失败，不静默跳过）。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def admin_session(migrated_database: None) -> AsyncIterator[AsyncSession]:
    """管理连接会话（可跨租户执行同步）；用例前清空业务数据。"""

    engine: AsyncEngine = make_test_engine(_ADMIN_ROLE)
    try:
        await clean_test_data(engine)
    finally:
        await engine.dispose()
    session_engine: AsyncEngine = make_test_engine(_ADMIN_ROLE)
    factory: async_sessionmaker[AsyncSession] = make_session_factory(session_engine)
    try:
        async with factory() as session:
            yield session
    finally:
        await session_engine.dispose()


async def _create_tenant(session: AsyncSession, name: str) -> str:
    tenant_id = await session.scalar(
        text(
            """
            insert into public.tenants (id, school_code, name, status)
            values (gen_random_uuid(), :code, :name, 'active')
            returning id::text
            """
        ),
        {"code": f"UI3{uuid4().hex[:8].upper()}", "name": name},
    )
    await session.commit()
    return str(tenant_id)


async def _insert_role(session: AsyncSession, tenant_id: str, code: str, *, is_system: bool) -> None:
    await session.execute(
        text(
            """
            insert into public.roles (id, tenant_id, code, name, is_system)
            values (gen_random_uuid(), cast(:tid as uuid), :code, :name, :is_system)
            """
        ),
        {"tid": tenant_id, "code": code, "name": f"{code} 名称", "is_system": is_system},
    )


async def _permission_exists(session: AsyncSession, code: str) -> bool:
    return (
        await session.scalar(text("select 1 from public.permissions where code = :code"), {"code": code})
        is not None
    )


async def test_factory_default_table_holds_no_sensitive_code_for_editable_roles(
    admin_session: AsyncSession,
) -> None:
    """库里的出厂默认表也不能给可编辑角色塞敏感码（`reset_role` 读的是这张表，不是代码目录）。

    为什么单独钉这张表：重置出厂按 `rbac_builtin_role_defaults` 重种权限，**不经过自扩权守卫**
    （没有 ack、没有 `school.role.self_grant`）。持有 `school.role.manage` 的人把自己所在角色
    重置一次就能整批换掉权限集，所以默认表里出现敏感码 = 开一条无审计的自扩权通道。
    代码侧的同一判据见 `tests/test_self_grant_guard_unit.py`（那份管 `seeds.py`，这份管同步落库
    的结果与人手插进定义表的行）。
    """

    await sync_rbac_definitions(admin_session)
    await admin_session.commit()
    rows = (
        (
            await admin_session.execute(
                text(
                    "select role_code, permission_code from public.rbac_builtin_role_defaults "
                    "where realm = 'tenant' and role_code = any(cast(:codes as text[])) "
                    "and permission_code = any(cast(:sensitive as text[]))"
                ),
                {
                    "codes": sorted(EDITABLE_ROLE_CODES),
                    "sensitive": sorted(SENSITIVE_SELF_GRANT_CODES),
                },
            )
        )
        .mappings()
        .all()
    )
    assert [(str(r["role_code"]), str(r["permission_code"])) for r in rows] == [], (
        f"出厂默认含敏感码，重置出厂会绕过 TD-191：{rows}"
    )


async def test_new_role_delete_code_syncs_and_reaches_super_admin(admin_session: AsyncSession) -> None:
    """目录新增码经同步落库；超管角色按「本校全量」被补到，不需要人工 SQL。"""

    tenant_id = await _create_tenant(admin_session, "自定义角色约束学校")
    await sync_rbac_definitions(admin_session)
    await admin_session.commit()

    assert await _permission_exists(admin_session, "school.role.delete"), (
        "新码没有随同步进 permissions 目录"
    )
    granted = await admin_session.scalar(
        text(
            """
            select count(*) from public.role_permissions rp
            join public.permissions p on p.id = rp.permission_id
            join public.roles r on r.id = rp.role_id
            where r.tenant_id = cast(:tid as uuid)
              and r.code = 'school_super_admin'
              and p.code = 'school.role.delete'
            """
        ),
        {"tid": tenant_id},
    )
    assert int(granted or 0) == 1, "超管没有拿到新码：全量型角色的「只补不撤」漏了新目录项"


async def test_custom_role_code_format_is_enforced_in_database(admin_session: AsyncSession) -> None:
    """坏格式（大写/短于 3/含连字符/超长）一律 IntegrityError；合法自定义 code 必须能插。"""

    tenant_id = await _create_tenant(admin_session, "角色码格式学校")

    for bad_code in ("Bad-Code", "ab", "role name", "x" * 64, "1leading_digit", ""):
        with pytest.raises(IntegrityError):
            await _insert_role(admin_session, tenant_id, bad_code, is_system=False)
        await admin_session.rollback()

    # 反证：约束不能把正常自定义角色一起挡掉
    await _insert_role(admin_session, tenant_id, "dean_office_staff", is_system=False)
    await admin_session.commit()
    assert (
        await admin_session.scalar(
            text("select name from public.roles where tenant_id = cast(:tid as uuid) and code = 'dean_office_staff'"),
            {"tid": tenant_id},
        )
        == "dean_office_staff 名称"
    )


async def test_builtin_role_codes_are_reserved_for_system_roles(admin_session: AsyncSession) -> None:
    """内置 code 只能由 `is_system=true` 持有；同步补种内置角色本身不能被自己的约束挡住。"""

    tenant_id = await _create_tenant(admin_session, "内置码保留学校")
    builtin_codes = [str(role.code) for role in TENANT_BUILTIN_ROLES]
    assert builtin_codes, "内置角色清单为空：本用例的前置不成立"

    for code in builtin_codes:
        with pytest.raises(IntegrityError):
            await _insert_role(admin_session, tenant_id, code, is_system=False)
        await admin_session.rollback()

    # 反证 1：内置角色由同步正常补种（约束不能挡开通链路）
    await sync_rbac_definitions(admin_session)
    await admin_session.commit()
    seeded = await admin_session.scalar(
        text(
            "select count(*) from public.roles where tenant_id = cast(:tid as uuid) and is_system"
        ),
        {"tid": tenant_id},
    )
    assert int(seeded or 0) == len(builtin_codes), (
        f"同步后内置角色应有 {len(builtin_codes)} 条，实际 {seeded}：保留名单写过头"
    )

    # 反证 2：非内置 code 的自定义角色仍然可建（与上一条用例互为边界）
    await _insert_role(admin_session, tenant_id, "library_admin", is_system=False)
    await admin_session.commit()
