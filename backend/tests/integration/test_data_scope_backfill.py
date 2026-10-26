"""M1-E2 集成测试：出厂数据范围如何落库（决策 ⑨、TD-138）。

钉住四条口径：

1. 内置角色的默认范围**来自代码声明**（`seeds.BuiltinRole.scopes` → 定义表 → `data_scopes`）；
2. 与权限映射同一"只补缺"口径：**角色已经有任何范围行就不再改写**——
   运营把 `school` 收窄成 `self` 后，重跑同步不得把全校加回来；
3. 角色被主动清空后**不再自动重补**；只有 `defaults_applied=false`（从未初始化）才补一次
   （TD-150）；新租户开通即带范围并标记为已初始化；
4. 范围解析是 fail-closed 的，所以"整租户一条范围都没有"必须在同步链路暴露，
   而不是静默交付一个"接口全 200、列表全空"的租户。
"""

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.rbac import (
    TENANT_WITHOUT_ANY_SCOPE_SQL,
    assert_every_tenant_has_scope,
    sync_rbac_definitions,
)
from app.rbac.seeds import TENANT_BUILTIN_ROLES
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
    """确保测试库结构最新（并做一次性测试库护栏）。"""

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


async def _create_tenant(session: AsyncSession, label: str) -> str:
    tenant_id = await session.scalar(
        text(
            "insert into public.tenants (id, school_code, name, status) "
            "values (gen_random_uuid(), :code, :name, 'active') returning id::text"
        ),
        {"code": f"SCH-SCOPE-{label}", "name": f"范围测试学校-{label}"},
    )
    await session.commit()
    return str(tenant_id)


async def _role_scopes(session: AsyncSession, role_code: str, tenant_id: str) -> set[str]:
    scopes: list[str] = list(
        (
            await session.execute(
                text(
                    "select s.scope_type from public.data_scopes s "
                    "join public.roles r on r.id = s.role_id "
                    "where r.code = :code and r.tenant_id = cast(:t as uuid)"
                ),
                {"code": role_code, "t": tenant_id},
            )
        ).scalars()
    )
    return set(scopes)


def _factory_scopes(role_code: str) -> frozenset[str]:
    return next(role.scopes for role in TENANT_BUILTIN_ROLES if role.code == role_code)


@pytest.mark.asyncio
async def test_sync_writes_declared_factory_scopes(admin_session: AsyncSession) -> None:
    """同步后：每个内置角色的范围行与代码声明逐角色相等（超管/管理员全校，其余本人）。"""

    tenant_id = await _create_tenant(admin_session, "S1")
    await sync_rbac_definitions(admin_session)
    await admin_session.commit()

    for role in TENANT_BUILTIN_ROLES:
        assert await _role_scopes(admin_session, role.code, tenant_id) == set(role.scopes), role.code
    # 出厂默认里必须真的出现 school 与 self 两种（防止声明被改成一边倒而测试仍绿）
    assert {scope for role in TENANT_BUILTIN_ROLES for scope in role.scopes} == {"school", "self"}


@pytest.mark.asyncio
async def test_provision_function_gives_new_tenant_scopes(admin_session: AsyncSession) -> None:
    """新租户开通（`seed_builtin_roles`）即带范围：不依赖下一次重启才补，首登就能看到数据。"""

    tenant_id = await _create_tenant(admin_session, "S2")
    await admin_session.execute(text("select public.seed_builtin_roles(cast(:t as uuid))"), {"t": tenant_id})
    await admin_session.commit()

    assert await _role_scopes(admin_session, "school_super_admin", tenant_id) == set(
        _factory_scopes("school_super_admin")
    )
    assert await _role_scopes(admin_session, "student", tenant_id) == {"self"}
    applied: list[Any] = list(
        (
            await admin_session.execute(
                text(
                    "select defaults_applied from public.roles "
                    "where tenant_id = cast(:t as uuid) and is_system"
                ),
                {"t": tenant_id},
            )
        )
        .scalars()
        .all()
    )
    assert applied and all(applied), "开通函数没有把内置角色标记为已初始化"


@pytest.mark.asyncio
async def test_sync_keeps_manually_narrowed_scope(admin_session: AsyncSession) -> None:
    """人工把管理员从 `school` 收窄成 `self`：重跑同步不得把全校加回来。"""

    tenant_id = await _create_tenant(admin_session, "S3")
    await sync_rbac_definitions(admin_session)
    await admin_session.commit()
    assert await _role_scopes(admin_session, "school_admin", tenant_id) == {"school"}

    await admin_session.execute(
        text(
            "update public.data_scopes s set scope_type = 'self' "
            "from public.roles r where s.role_id = r.id and r.code = 'school_admin' "
            "and r.tenant_id = cast(:t as uuid)"
        ),
        {"t": tenant_id},
    )
    await admin_session.commit()

    await sync_rbac_definitions(admin_session)
    await admin_session.commit()

    assert await _role_scopes(admin_session, "school_admin", tenant_id) == {"self"}


@pytest.mark.asyncio
async def test_sync_does_not_reapply_scopes_after_role_is_emptied(
    admin_session: AsyncSession,
) -> None:
    """TD-150：范围被整体撤空后同步不得补回；显式标记未初始化才补一次。"""

    tenant_id = await _create_tenant(admin_session, "S4")
    await sync_rbac_definitions(admin_session)
    await admin_session.commit()

    await admin_session.execute(
        text(
            "delete from public.data_scopes s "
            "using public.roles r where s.role_id = r.id and r.code = 'teacher_b_end' "
            "and r.tenant_id = cast(:t as uuid)"
        ),
        {"t": tenant_id},
    )
    await admin_session.commit()
    assert await _role_scopes(admin_session, "teacher_b_end", tenant_id) == set()

    await sync_rbac_definitions(admin_session)
    await admin_session.commit()
    assert await _role_scopes(admin_session, "teacher_b_end", tenant_id) == set(), (
        "主动撤空的范围被同步补回了（TD-150 回归）"
    )

    await admin_session.execute(
        text(
            "update public.roles set defaults_applied = false "
            "where code = 'teacher_b_end' and tenant_id = cast(:t as uuid)"
        ),
        {"t": tenant_id},
    )
    await admin_session.commit()
    await sync_rbac_definitions(admin_session)
    await admin_session.commit()

    assert await _role_scopes(admin_session, "teacher_b_end", tenant_id) == set(
        _factory_scopes("teacher_b_end")
    ), "标记为未初始化时没有补出厂范围"


@pytest.mark.asyncio
async def test_tenant_without_any_scope_is_caught(admin_session: AsyncSession) -> None:
    """整租户没有范围行必须被查出来并 fail-fast（口径 ⑨ 的空集语义不容静默）。"""

    tenant_id = await _create_tenant(admin_session, "S5")
    await sync_rbac_definitions(admin_session)
    await admin_session.commit()

    # 正面：同步过的租户不在"没有任何范围"的清单里
    synced = (await admin_session.execute(text(TENANT_WITHOUT_ANY_SCOPE_SQL))).all()
    assert tenant_id not in [str(row[0]) for row in synced]

    await admin_session.execute(
        text("delete from public.data_scopes where tenant_id = cast(:t as uuid)"),
        {"t": tenant_id},
    )
    await admin_session.commit()

    emptied = [str(row[0]) for row in (await admin_session.execute(text(TENANT_WITHOUT_ANY_SCOPE_SQL))).all()]
    assert emptied == [tenant_id]
    with pytest.raises(RuntimeError, match="没有任何 data_scopes 行"):
        assert_every_tenant_has_scope(emptied)
