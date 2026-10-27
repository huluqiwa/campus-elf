"""自定义角色的库层约束与目录落库（M2-UI3 TD-187a，TD-187c 追加第 4 条）。

钉住四件事（简报 §4.1 + §11 决策 #6 + §13.2 决策③）：

1. 新权限码 `school.role.delete` 随代码目录同步落库，且校方超管按「本校全量」补到——
   目录加了码但超管没补上 = 新能力只有手写 SQL 才能授出；
2. `roles.code` 格式约束：只有 `^[a-z][a-z0-9_]{2,62}$` 进得了库。接口层的正则会随重构漂移，
   库层这条是「绕过应用代码的手工 SQL」的最后防线；
3. 内置角色码保留：非系统角色不得占用内置 code。靠「本租户恰好已有同码内置行」的唯一约束
   不够——那是逐租户偶然成立，跨租户一致性必须由 CHECK 声明。
4. `data_scopes` 两条 CHECK 的**全名**与代码里引用的一致：范围替换的漂移分支按名字等值比较，
   命名约定加过前缀，写错就是"人读 409 变纯文本 500"；`scope_id` 那条还是 422 文案的事实来源。

每条都配反证（合法值必须能插入），否则约束可能是空挡或写过头。
"""

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.rbac import admin, sync_rbac_definitions
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

#: `data_scopes` 里那条"class/course 必须带 scope_id"的 CHECK 全名（库里的事实，不是代码常量）。
#: `admin.py` 的 422 文案引用它，所以这里既钉名字、也钉"缺 id 真会被挡"这条硬理由。
_SCOPE_ID_CHECK_CONSTRAINT = "ck_data_scopes_scope_id_required_for_class_course"


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


async def test_data_scope_check_constraint_names_match_the_database(
    admin_session: AsyncSession,
) -> None:
    """`admin.py` 里引用的两条 `data_scopes` CHECK 必须与库里的**全名**一致（TD-187c）。

    为什么要单独钉：
    - 范围内插的漂移分支按约束名做**等值比较**。SQLAlchemy 的命名约定给模型里声明的名字加
      `ck_<table>_` 前缀，驱动回报的是前缀后的完整名；写成 `scope_type_valid` 会让"翻成人读 409"
      的分支变成死代码，实际表现是纯文本 500——而这正是本项目最忌讳的"把业务拒绝伪装成故障"。
    - `ck_data_scopes_scope_id_required_for_class_course` 是" `class`/`course` 不可经角色配置写入"
      这条**对外文案**的事实来源。库层没有这条约束，422 的理由就是说谎（运营会照着一个不成立的原因去排查）。
    反证：同一张表能插入合法的 `class` 行（带 id），说明约束不是空挡、也没有写过头。
    """

    rows: list[Any] = list(
        (
            await admin_session.execute(
                text(
                    "select con.conname from pg_constraint con "
                    "join pg_class cls on cls.oid = con.conrelid "
                    "where cls.relname = 'data_scopes' and con.contype = 'c'"
                )
            )
        )
        .scalars()
        .all()
    )
    names = {str(row) for row in rows}
    assert admin._SCOPE_TYPE_CHECK_CONSTRAINT in names, (
        f"范围内插漂移分支会失配（等值比较落空 → 业务拒绝变成 500）：库里是 {sorted(names)}"
    )
    assert _SCOPE_ID_CHECK_CONSTRAINT in names, "422 文案引用的约束在库里不存在"

    tenant_id = await _create_tenant(admin_session, "范围 CHECK 学校")
    await _insert_role(admin_session, tenant_id, "scope_check_role", is_system=False)
    await admin_session.commit()
    role_id = await admin_session.scalar(
        text("select id from public.roles where tenant_id = cast(:tid as uuid) and code = 'scope_check_role'"),
        {"tid": tenant_id},
    )

    # 反证 1：合法的 class 行（带具体班级 id）必须进得了库
    await admin_session.execute(
        text(
            "insert into public.data_scopes (id, tenant_id, role_id, scope_type, scope_id) "
            "values (gen_random_uuid(), cast(:tid as uuid), cast(:rid as uuid), 'class', gen_random_uuid())"
        ),
        {"tid": tenant_id, "rid": str(role_id)},
    )
    await admin_session.commit()

    # 正证：缺 scope_id 的 class 行被 CHECK 挡下 —— 这就是角色配置端点拒收它的硬理由
    with pytest.raises(IntegrityError):
        await admin_session.execute(
            text(
                "insert into public.data_scopes (id, tenant_id, role_id, scope_type, scope_id) "
                "values (gen_random_uuid(), cast(:tid as uuid), cast(:rid as uuid), 'class', null)"
            ),
            {"tid": tenant_id, "rid": str(role_id)},
        )
    await admin_session.rollback()
