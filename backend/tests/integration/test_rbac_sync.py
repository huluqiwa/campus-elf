"""RBAC 定义同步集成测试（真实 PostgreSQL）。

钉住 M1-E1 的两层口径（`docs/design-briefs/M1-E1-rbac-core.md` §3.1/§5.1）：

1. 权限项目录跟代码走：`permissions` upsert 后与 catalog 逐码相等；
2. 角色↔权限映射是运营配置：同步**只补缺失**，人工增删的映射保持原样（跑两次也保持）；
3. 与矩阵重复的 9 个旧码被删除，且不留下悬空映射；
4. 内置角色对全部租户补齐（含 C 端 `student`/`teacher`）；
5. 内置角色码撞上运营自建（非系统）角色时 fail-fast，不覆盖人工数据。
"""

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.rbac import sync_rbac_definitions
from app.rbac.admin import EDITABLE_ROLE_CODES
from app.rbac.catalog import catalog_codes, codes_with_module, legacy_codes_removed_by_m1e1
from app.rbac.seeds import TENANT_BUILTIN_ROLES, builtin_role
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
    """管理连接会话（超级用户，可跨租户执行同步）；用例前清空业务数据。"""

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


async def _create_tenant(session: AsyncSession, name: str = "同步测试学校") -> str:
    """建一个租户，返回 id 字符串。"""

    tenant_id = await session.scalar(
        text(
            """
            insert into public.tenants (id, school_code, name, status)
            values (gen_random_uuid(), :code, :name, 'active')
            returning id::text
            """
        ),
        {"code": f"SYNC{abs(hash((name, id(session)))) % 100000:05d}", "name": name},
    )
    await session.commit()
    return str(tenant_id)


async def _role_permissions(session: AsyncSession, role_code: str) -> set[str]:
    """某角色码在全部租户下的权限码集合（role_code, permission_code 去重）。"""

    rows: set[str] = set(
        (
            await session.execute(
                text(
                    """
                    select distinct p.code
                    from public.roles r
                    join public.role_permissions rp on rp.role_id = r.id
                    join public.permissions p on p.id = rp.permission_id
                    where r.code = :code
                    """
                ),
                {"code": role_code},
            )
        ).scalars()
    )
    return rows


async def test_sync_aligns_catalog_and_creates_builtin_roles(admin_session: AsyncSession) -> None:
    """同步后：目录与代码逐码相等、旧码清空、每个租户五个内置角色齐备且默认映射正确。"""

    tenant_id = await _create_tenant(admin_session)

    assert await sync_rbac_definitions(admin_session) > 0
    await admin_session.commit()

    codes: set[str] = set(
        (await admin_session.execute(text("select code from public.permissions"))).scalars()
    )
    assert codes == set(catalog_codes())
    legacy = set(legacy_codes_removed_by_m1e1())
    assert not (codes & legacy), f"旧码未删除：{sorted(codes & legacy)}"

    role_codes: set[str] = set(
        (
            await admin_session.execute(
                text("select code from public.roles where tenant_id = cast(:t as uuid)"),
                {"t": tenant_id},
            )
        ).scalars()
    )
    assert role_codes == {role.code for role in TENANT_BUILTIN_ROLES}

    # 超管＝本校全量；管理员/教师/C 端按出厂默认
    assert await _role_permissions(admin_session, "school_super_admin") == (
        codes_with_module("school") | codes_with_module("campus")
    )
    assert await _role_permissions(admin_session, "school_admin") == builtin_role(
        "tenant", "school_admin"
    ).permissions
    assert "school.kb.upload" in await _role_permissions(admin_session, "teacher_b_end")
    assert "campus.leave.create" in await _role_permissions(admin_session, "student")
    assert "campus.leave.create" not in await _role_permissions(admin_session, "teacher")

    # 旧码不留悬空映射
    dangling = await admin_session.scalar(
        text(
            """
            select count(*) from public.role_permissions rp
            join public.permissions p on p.id = rp.permission_id
            where p.code = any(cast(:codes as text[]))
            """
        ),
        {"codes": list(legacy)},
    )
    assert dangling == 0


async def test_sync_is_idempotent_and_keeps_manual_edits(admin_session: AsyncSession) -> None:
    """同步幂等，且**不覆盖运营的人工改动**（撤销的映射不补回、额外授予的保留）。"""

    await _create_tenant(admin_session)
    await sync_rbac_definitions(admin_session)
    await admin_session.commit()

    admin_role_id = await _create_extra_admin_role(admin_session)

    # 人工改动 1：撤销 school_admin 的 school.kb.view
    await admin_session.execute(
        text(
            """
            delete from public.role_permissions rp
            using public.permissions p
            where rp.permission_id = p.id
              and rp.role_id = cast(:role as uuid)
              and p.code = 'school.kb.view'
            """
        ),
        {"role": admin_role_id},
    )
    # 人工改动 2：额外授予矩阵里标 △ 的 KB 发布权限
    await admin_session.execute(
        text(
            """
            insert into public.role_permissions (id, tenant_id, role_id, permission_id)
            select gen_random_uuid(), r.tenant_id, r.id, p.id
            from public.roles r
            join public.permissions p on p.code = 'school.kb.publish'
            where r.id = cast(:role as uuid)
            """
        ),
        {"role": admin_role_id},
    )
    await admin_session.commit()

    before = await _permission_snapshot(admin_session)

    await sync_rbac_definitions(admin_session)
    await sync_rbac_definitions(admin_session)  # 再跑两次：幂等
    await admin_session.commit()

    assert await _permission_snapshot(admin_session) == before
    grants = await _role_permissions(admin_session, "school_admin")
    assert "school.kb.publish" in grants, "人工额外授予的权限被同步删掉了"
    assert "school.kb.view" not in grants, "人工撤销的权限被同步补回来了"


async def _create_extra_admin_role(session: AsyncSession) -> str:
    """返回其中一个租户的 school_admin 角色 id（多租户时用第一个，仅用于改映射）。"""

    role_id = await session.scalar(
        text("select id::text from public.roles where code = 'school_admin' limit 1")
    )
    assert role_id is not None
    return str(role_id)


async def _permission_snapshot(session: AsyncSession) -> set[str]:
    """全部内置角色映射的 `tenant_id|role_code|permission_code` 集合。"""

    rows: set[str] = set(
        (
            await session.execute(
                text(
                    """
                    select r.tenant_id::text || '|' || r.code || '|' || p.code
                    from public.roles r
                    join public.role_permissions rp on rp.role_id = r.id
                    join public.permissions p on p.id = rp.permission_id
                    where r.is_system
                    """
                )
            )
        ).scalars()
    )
    return rows


async def test_sync_refuses_to_touch_non_system_role_with_builtin_code(
    admin_session: AsyncSession,
) -> None:
    """内置角色码撞上运营自建角色（is_system=false）→ fail-fast，不动它的数据。"""

    tenant_id = await _create_tenant(admin_session, "自建角色学校")
    await admin_session.execute(
        text(
            """
            insert into public.roles (id, tenant_id, code, name, is_system)
            values (gen_random_uuid(), cast(:t as uuid), 'school_admin', '校方自建管理员', false)
            """
        ),
        {"t": tenant_id},
    )
    await admin_session.commit()

    # 前置检查失败即不执行任何语句：同步前后的角色映射不变
    snapshot_before = await _permission_snapshot(admin_session)
    with pytest.raises(RuntimeError, match="非系统角色"):
        await sync_rbac_definitions(admin_session)
    await admin_session.rollback()
    assert await _permission_snapshot(admin_session) == snapshot_before


async def test_sync_refuses_to_delete_legacy_codes_still_granted_to_operator_role(
    admin_session: AsyncSession,
) -> None:
    """口径 ⑧ 的前置闸门：旧码仍被非全量型角色引用时 fail-fast，绝不静默删映射（审查 M4）。

    简报 §5.1-① 承诺"只在没有引用时删除"。若运营自建角色恰好持有 `school.user.view`，
    同步把它删掉 = 该角色第二天凭空少一条权限、且无人知晓。所以这里必须报错并原样保留数据。
    """

    tenant_id = await _create_tenant(admin_session, "旧码引用学校")
    # 模拟 E1 之前的库状态：旧码仍在目录里，且被一个运营自建（非系统）角色引用
    await admin_session.execute(
        text(
            "insert into public.permissions (code, module, action) "
            "values ('school.user.view', 'school', 'view') on conflict (code) do nothing"
        )
    )
    role_id = await admin_session.scalar(
        text(
            "insert into public.roles (id, tenant_id, code, name, is_system) "
            "values (gen_random_uuid(), cast(:t as uuid), 'kb_only', '运营自建角色', false) "
            "returning id::text"
        ),
        {"t": tenant_id},
    )
    await admin_session.execute(
        text(
            "insert into public.role_permissions (id, tenant_id, role_id, permission_id) "
            "select gen_random_uuid(), cast(:t as uuid), cast(:r as uuid), p.id "
            "from public.permissions p where p.code = 'school.user.view'"
        ),
        {"t": tenant_id, "r": str(role_id)},
    )
    await admin_session.commit()

    try:
        with pytest.raises(RuntimeError, match="旧码仍被"):
            await sync_rbac_definitions(admin_session)
        await admin_session.rollback()

        still_granted = await admin_session.scalar(
            text(
                "select count(*) from public.role_permissions rp "
                "join public.roles r on r.id = rp.role_id "
                "join public.permissions p on p.id = rp.permission_id "
                "where r.code = 'kb_only' and p.code = 'school.user.view'"
            )
        )
        assert still_granted == 1, "前置检查失败却已经把运营映射删掉了"
    finally:
        # 复位：这条用例故意造出的旧码与自建角色不能留给其它用例（目录相等性断言会红）。
        # 用独立引擎：admin_session 的事务已被 rollback，但它仍持有连接。
        cleanup_engine = make_test_engine(_ADMIN_ROLE)
        try:
            async with cleanup_engine.begin() as conn:
                await conn.execute(
                    text(
                        "delete from public.role_permissions where role_id in "
                        "(select id from public.roles where code = 'kb_only')"
                    )
                )
                await conn.execute(text("delete from public.roles where code = 'kb_only'"))
                await conn.execute(
                    text("delete from public.tenants where id = cast(:t as uuid)"), {"t": tenant_id}
                )
                await conn.execute(text("delete from public.permissions where code = 'school.user.view'"))
        finally:
            await cleanup_engine.dispose()


async def test_sync_does_not_reapply_defaults_after_role_is_emptied(
    admin_session: AsyncSession,
) -> None:
    """TD-150：清空内置角色后同步不得补回；仅显式标记 `defaults_applied=false` 时补一次。

    `teacher_b_end` 的出厂默认只有 `school.kb.upload` 一条，撤销最后一条等价于清空——
    旧启发式会把这条默认补回来，让运营以为“撤销没保存”。现在由标记区分
    “从未初始化”与“主动清空”。
    """

    await _create_tenant(admin_session, "清空角色学校")
    await sync_rbac_definitions(admin_session)
    await admin_session.commit()

    role_id = await admin_session.scalar(
        text("select id::text from public.roles where code = 'teacher_b_end' limit 1")
    )
    assert role_id is not None
    applied = await admin_session.scalar(
        text("select defaults_applied from public.roles where id = cast(:r as uuid)"),
        {"r": str(role_id)},
    )
    assert applied is True, "同步后出厂默认未标记为已初始化"

    await admin_session.execute(
        text("delete from public.role_permissions where role_id = cast(:r as uuid)"),
        {"r": str(role_id)},
    )
    await admin_session.commit()
    assert await _role_permissions(admin_session, "teacher_b_end") == set()

    await sync_rbac_definitions(admin_session)
    await admin_session.commit()
    assert await _role_permissions(admin_session, "teacher_b_end") == set(), (
        "主动清空的内置角色被同步补回了出厂默认（TD-150 回归）"
    )

    # 模拟“从未初始化”：只有显式把标记翻回 false，才允许补一次。
    await admin_session.execute(
        text(
            "update public.roles set defaults_applied = false where id = cast(:r as uuid)"
        ),
        {"r": str(role_id)},
    )
    await admin_session.commit()
    await sync_rbac_definitions(admin_session)
    await admin_session.commit()

    assert await _role_permissions(admin_session, "teacher_b_end") == set(
        builtin_role("tenant", "teacher_b_end").permissions
    ), "标记为未初始化时没有补出厂默认"
    applied = await admin_session.scalar(
        text("select defaults_applied from public.roles where id = cast(:r as uuid)"),
        {"r": str(role_id)},
    )
    assert applied is True, "补授后未把标记置回 true"


async def test_sync_allows_legacy_codes_referenced_only_by_full_scope_roles(
    admin_session: AsyncSession,
) -> None:
    """前置闸门只放行全量型角色（超管旧映射按模块全量补，删掉不丢能力）。

    若不放行，任何库里残留旧码超管映射的环境都会在同步入口被卡死；但放行的前提是
    「删了也不丢能力」——所以这里同时断言超管同步后仍是 school+campus 全量。
    """

    await _create_tenant(admin_session, "超管旧码学校")
    await sync_rbac_definitions(admin_session)
    await admin_session.commit()

    # 模拟 E1 之前的库状态：旧码仍在目录里，且已被授给系统超管角色
    await admin_session.execute(
        text(
            "insert into public.permissions (code, module, action) "
            "values ('school.user.view', 'school', 'view') on conflict (code) do nothing"
        )
    )
    role_id = await admin_session.scalar(
        text("select id::text from public.roles where code = 'school_super_admin' limit 1")
    )
    assert role_id is not None
    await admin_session.execute(
        text(
            "insert into public.role_permissions (id, tenant_id, role_id, permission_id) "
            "select gen_random_uuid(), r.tenant_id, r.id, p.id "
            "from public.roles r join public.permissions p on p.code = 'school.user.view' "
            "where r.id = cast(:role as uuid) "
            "on conflict do nothing"
        ),
        {"role": str(role_id)},
    )
    await admin_session.commit()
    assert await _role_permissions(admin_session, "school_super_admin") & {"school.user.view"} == {
        "school.user.view"
    }, "用例前置条件未成立：超管角色并没有旧码映射"

    await sync_rbac_definitions(admin_session)  # 不应抛错
    await admin_session.commit()

    leftover = await admin_session.scalar(
        text("select count(*) from public.permissions where code = 'school.user.view'")
    )
    assert leftover == 0, "全量型角色的旧码引用被闸门误挡，或旧码没按口径删除"
    assert await _role_permissions(admin_session, "school_super_admin") == (
        codes_with_module("school") | codes_with_module("campus")
    ), "删掉旧码映射后，超管的全量范围没有按口径补回"


async def test_builtin_defaults_tables_never_reference_missing_permissions(
    admin_session: AsyncSession,
) -> None:
    """重置出厂的重种是 `defaults join permissions`：定义表引用缺码时 join 会**静默少种**。
    `rbac_builtin_role_defaults` / `role_permissions` 对 `permissions` 都是 ON DELETE CASCADE，
    所以这个状态在库里不存在（删一行会连带删掉引用它的定义与其它租户映射）。
    `reset_role` 仍加了条数复核兜住"将来有人把 FK 改松"，本用例钉住的是它今天依赖的不变量。
    """

    tenant_id = await _create_tenant(admin_session, "出厂定义完整性学校")
    await sync_rbac_definitions(admin_session)
    await admin_session.commit()

    orphan_defaults = await admin_session.scalar(
        text(
            "select count(*) from public.rbac_builtin_role_defaults d "
            "left join public.permissions p on p.code = d.permission_code "
            "where p.code is null"
        )
    )
    assert orphan_defaults == 0, "出厂权限定义引用了库里不存在的权限码：重置会静默少种"

    # 可重置角色的出厂定义必须真的能 join 出权限行（条数与 join 结果一致=重种不会少）
    for role_code in sorted(EDITABLE_ROLE_CODES):
        defined = await admin_session.scalar(
            text(
                "select count(*) from public.rbac_builtin_role_defaults "
                "where realm = 'tenant' and role_code = :code"
            ),
            {"code": role_code},
        )
        resolvable = await admin_session.scalar(
            text(
                "select count(*) from public.rbac_builtin_role_defaults d "
                "join public.permissions p on p.code = d.permission_code "
                "where d.realm = 'tenant' and d.role_code = :code"
            ),
            {"code": role_code},
        )
        assert int(defined) == int(resolvable) > 0, (
            f"{role_code} 出厂定义 {defined} 条、可解析 {resolvable} 条"
        )
        scopes = await admin_session.scalar(
            text(
                "select count(*) from public.rbac_builtin_role_scope_defaults "
                "where realm = 'tenant' and role_code = :code"
            ),
            {"code": role_code},
        )
        assert int(scopes) > 0, f"{role_code} 没有出厂数据范围，重置后无人能看到数据"
    assert tenant_id is not None
