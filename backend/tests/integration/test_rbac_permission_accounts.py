"""权限账号列表 / 可绑定角色目录 / `resettable` 派生（TD-187g 决策 1、2 与 TD-200、TD-198）。

这三条能力都是前端 TD-187g-B / TD-187g-C 的**前置契约**，各自钉住一件"前端如果自己猜就会错"的事：

1. **权限账号列表**（`list_permission_accounts`）：按账号类型过滤（教师 + 校方管理员，与绑定写路径
   同一集合），学生与 `deleted` 不列，跨租不列；每行带**已绑角色码**与 `roles_version`
   （绑定页要拿它做 CAS）；**不回显 `login_id`**（学号/工号属敏感标识符）；带服务端派生的
   `is_super_admin`（超管账号的角色绑定租户内改不动，前端据此关掉绑定入口，TD-242）。
   列表**不按人员数据范围裁剪**——权限账号管理是租户级操作，按 `school`/`self` 裁会让管理员
   看不见自己要管的账号。
2. **可绑定角色目录**（`list_bindable_roles`）：超管角色**不列**（列出来 = 能勾不能存）；
   形状里**没有权限码**——完整权限矩阵是 `school.role.manage` 的事，只持
   `school.permission_account.manage` 的人不该顺手拿到它。
3. **`resettable`**（TD-200）：由服务端从出厂默认表派生，与 `is_system` **不是一回事**。
   前端按它点亮"恢复出厂"；按 `is_system` 点亮的按钮在"内置但撤了定义"时会亮着恒 409。
4. **角色名非空白**（TD-198）：绕过应用代码的手工 SQL 也写不进一行没名字的角色。

不测 HTTP 层：权限码的 403 由 `test_route_permission_manifest_unit.py` 与业务场景用例覆盖，
这里测的是"服务层按什么口径取数"。
"""

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth.principal import Principal
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.rbac import admin
from app.rbac.binding import list_bindable_roles, list_permission_accounts
from tests.support.auth import seed_tenant_user
from tests.support.db import clean_test_data, database_url, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn

pytestmark = pytest.mark.integration

PASSWORD = "PermAccount-Pass-1"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def stack(migrated_database: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
    finally:
        await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    get_settings.cache_clear()
    yield
    await dispose_engine()
    get_settings.cache_clear()


def _principal(user_id: UUID, tenant_id: UUID) -> Principal:
    return Principal(
        user_id=user_id,
        realm="tenant",
        tenant_id=tenant_id,
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        must_change_password=False,
        roles=frozenset({"school_super_admin"}),
        permissions=frozenset({"school.permission_account.manage"}),
    )


async def _seed(school_code: str) -> dict[str, Any]:
    """一租户：超管 + 教师 + 校方管理员 + 学生 + 另一个租户的账号。"""

    engine = make_test_engine("admin")
    try:
        tenant_id, super_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"pa-super-{school_code}",
            password=PASSWORD,
            user_type="school_admin",
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text("select seed_builtin_roles(:tid)"), {"tid": str(tenant_id)}
            )
            await session.execute(
                text(
                    "insert into public.user_roles (tenant_id, user_id, role_id) "
                    "select :tid, :uid, r.id from public.roles r "
                    "where r.tenant_id = :tid and r.code = 'school_super_admin'"
                ),
                {"tid": str(tenant_id), "uid": str(super_id)},
            )
        _, teacher_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"pa-teacher-{school_code}",
            password=PASSWORD,
            user_type="teacher",
        )
        _, keeper_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"pa-keeper-{school_code}",
            password=PASSWORD,
            user_type="school_admin",
        )
        _, student_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"pa-student-{school_code}",
            password=PASSWORD,
            user_type="student",
        )
        _, deleted_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"pa-deleted-{school_code}",
            password=PASSWORD,
            user_type="teacher",
            status="deleted",
        )
        foreign_tenant_id, foreign_id = await seed_tenant_user(
            engine,
            school_code=f"{school_code}X",
            login_id=f"pa-foreign-{school_code}",
            password=PASSWORD,
            user_type="teacher",
        )
    finally:
        await engine.dispose()

    return {
        "tenant_id": tenant_id,
        "super_id": super_id,
        "teacher_id": teacher_id,
        "keeper_id": keeper_id,
        "student_id": student_id,
        "deleted_id": deleted_id,
        "foreign_tenant_id": foreign_tenant_id,
        "foreign_id": foreign_id,
        "principal": _principal(super_id, tenant_id),
    }


async def _bind(tenant_id: UUID, user_id: UUID, role_code: str) -> None:
    async with db_session(tenant_id=tenant_id) as session:
        await session.execute(
            text(
                "insert into public.user_roles (tenant_id, user_id, role_id) "
                "select :tid, :uid, r.id from public.roles r "
                "where r.tenant_id = :tid and r.code = :code"
            ),
            {"tid": str(tenant_id), "uid": str(user_id), "code": role_code},
        )


async def test_permission_accounts_list_filters_by_type_and_tenant(stack: None) -> None:
    """列表只给"能被绑角色"的账号（教师 + 校方管理员），学生与已删除账号不列，跨租不列。"""

    seeded = await _seed("pa-list")
    tenant_id = seeded["tenant_id"]
    await _bind(tenant_id, seeded["teacher_id"], "teacher_b_end")
    await _bind(tenant_id, seeded["keeper_id"], "school_admin")

    page = await list_permission_accounts(seeded["principal"], limit=50)

    ids = {item.id for item in page.items}
    assert seeded["super_id"] in ids
    assert seeded["teacher_id"] in ids
    assert seeded["keeper_id"] in ids
    assert seeded["student_id"] not in ids, "学生是 C 端账号，不在这条权限的射程内"
    assert seeded["deleted_id"] not in ids, "`deleted` 在绑定写路径里被当作不存在"
    assert seeded["foreign_id"] not in ids, "别的租户的账号不得出现"

    teacher = next(item for item in page.items if item.id == seeded["teacher_id"])
    assert teacher.user_type == "teacher"
    assert teacher.status == "active"
    assert teacher.role_codes == ("teacher_b_end",), "角色码是绑定页要显示的绑定结果"
    assert teacher.roles_version == 0, "没绑过角色的账号 `roles_version` 是 0（绑定 CAS 的起点）"
    keeper = next(item for item in page.items if item.id == seeded["keeper_id"])
    assert keeper.role_codes == ("school_admin",)

    # 超管账号**会**出现在列表里（它是 `school_admin` 类型），但服务端要把它标出来：
    # 它的角色绑定由平台开通流程管理，租户内改绑必然 403 `BINDING_SUPER_ADMIN_FORBIDDEN`，
    # 前端据此关掉绑定入口（TD-242）。这个位由服务端算，前端不按角色码自己认。
    super_account = next(item for item in page.items if item.id == seeded["super_id"])
    assert super_account.is_super_admin is True, "持有超管角色的账号必须被标出来"
    assert "school_super_admin" in super_account.role_codes
    assert teacher.is_super_admin is False
    assert keeper.is_super_admin is False

    # 不回显登录名：字段集合是接口契约的一部分（学号/工号属敏感标识符）
    assert not hasattr(teacher, "login_id")


async def test_permission_accounts_cursor_pagination_has_no_overlap(stack: None) -> None:
    """cursor 分页不重不漏：翻到最后一页收集到的账号集合**等于**全量，且没有重复。

    只断言"两页不相交"是不够的——第二页查空或跳过整段时，"不相交"照样成立。这里把
    每一页都翻完（`next_cursor is None` 为止），再与全量集合逐个比对。

    空页是**允许**的，而且只能是最后一页：仓库约定「取满一页才给游标」
    （`app/db/repository.py:list_cursor`），总数恰好是页长整数倍时，游标会被乐观地多发一次，
    下一次查询取回空页并终止。所以判据不是"不能有空页"，而是"空页之后不再有游标"。
    """

    seeded = await _seed("pa-page")
    engine = make_test_engine("admin")
    try:
        for _ in range(3):
            await seed_tenant_user(
                engine,
                school_code="pa-page",
                login_id=f"pa-extra-{uuid4().hex[:8]}",
                password=PASSWORD,
                user_type="teacher",
            )
    finally:
        await engine.dispose()

    everything = await list_permission_accounts(seeded["principal"], limit=50)
    all_ids = {item.id for item in everything.items}
    assert len(all_ids) > 2, "样本太少，翻不出多页，这条用例就废了"

    collected: list[UUID] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = await list_permission_accounts(seeded["principal"], limit=2, cursor=cursor)
        if not page.items:
            assert page.next_cursor is None, "空页还带游标：翻页在原地打转"
            break
        collected.extend(item.id for item in page.items)
        pages += 1
        cursor = page.next_cursor
        if cursor is None:
            break
        assert pages < 20, "翻页没有终止：游标在原地打转"

    assert len(collected) == len(set(collected)), "同一账号在两页里出现（游标推进错了）"
    assert set(collected) == all_ids, (
        f"翻页漏了账号：{len(all_ids) - len(set(collected))} 个不在翻页结果里"
    )
    assert pages > 1, "样本不够多页，这条用例证明不了翻页行为"


async def test_bindable_roles_excludes_super_admin_and_carries_no_permission_codes(
    stack: None,
) -> None:
    """可绑定目录不列超管角色；形状里没有权限码（那是 `school.role.manage` 的事）。"""

    seeded = await _seed("pa-bindable")
    await admin.create_role(
        seeded["principal"],
        code="library_keeper",
        name="图书馆管理员",
        permission_codes=["school.kb.view"],
        copy_from_role_id=None,
        reason=None,
        request_id=None,
    )

    roles = await list_bindable_roles(seeded["principal"])
    codes = {role.code for role in roles}

    assert "school_super_admin" not in codes, "超管角色不可经绑定接口授出，列出来=能勾不能存"
    assert "library_keeper" in codes, "自定义角色可绑"
    assert "school_admin" in codes
    for role in roles:
        assert not hasattr(role, "permission_codes"), (
            "绑定目录带上了权限码清单：只持账号管理权的人不该拿到完整权限矩阵"
        )


async def test_resettable_is_derived_from_factory_defaults_not_is_system(stack: None) -> None:
    """`resettable` 看的是"出厂默认表里有没有这个码"**加**"角色可不可编辑"，不是"是不是内置角色"。"""

    seeded = await _seed("pa-resettable")

    builtin = next(
        role
        for role in await admin.list_roles(seeded["principal"])
        if role.code == "school_admin"
    )
    assert builtin.resettable is True, "内置可编辑角色在出厂默认表里有定义行 → 可重置"

    # 锁定角色（校方超管/学生/教师）：**有**出厂定义，但重置先撞 `_assert_editable` → 409，
    # 所以按钮不能亮。只算"有定义"会给这三个角色点亮一个永远 409 的按钮（TD-200 要治的病）。
    locked = {
        role.code: role.resettable
        for role in await admin.list_roles(seeded["principal"])
        if role.code in {"school_super_admin", "student", "teacher"}
    }
    assert locked, "内置锁定角色没列出来：种子数据变了？"
    for code, resettable in locked.items():
        assert resettable is False, f"{code} 是锁定角色，重置永远 409：按钮不能亮"

    custom = await admin.create_role(
        seeded["principal"],
        code="library_keeper",
        name="图书馆管理员",
        permission_codes=["school.kb.view"],
        copy_from_role_id=None,
        reason=None,
        request_id=None,
    )
    assert custom.resettable is False, "自定义角色没有出厂定义 → 不可重置（否则按钮亮着恒 409）"

    # 反证：把内置角色的定义行删掉 → `resettable` 变 false，而 `is_system` 仍是 true。
    # 这一步证明两个字段确实不是同一件事（前端按 is_system 点亮按钮就会亮着恒 409）。
    #
    # 定义表是**全局**的（代码所有，不按租户分片），且租户连接角色只有 SELECT 权限：
    # 所以删除走 admin 连接，并且**必须原样恢复**——这张表不在 `clean_test_data` 的清理清单里，
    # 漏恢复会污染后面所有测试文件的出厂默认（表现为别的用例莫名其妙拿不到默认权限）。
    engine = make_test_engine("admin")
    permission_rows: list[str] = []
    scope_rows: list[str] = []
    try:
        async with engine.begin() as conn:
            permission_rows = list(
                (
                    await conn.execute(
                        text(
                            "select permission_code from public.rbac_builtin_role_defaults "
                            "where realm = 'tenant' and role_code = 'school_admin'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            scope_rows = list(
                (
                    await conn.execute(
                        text(
                            "select scope_type from public.rbac_builtin_role_scope_defaults "
                            "where realm = 'tenant' and role_code = 'school_admin'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            await conn.execute(
                text(
                    "delete from public.rbac_builtin_role_defaults "
                    "where realm = 'tenant' and role_code = 'school_admin'"
                )
            )
            await conn.execute(
                text(
                    "delete from public.rbac_builtin_role_scope_defaults "
                    "where realm = 'tenant' and role_code = 'school_admin'"
                )
            )

        after = next(
            role
            for role in await admin.list_roles(seeded["principal"])
            if role.code == "school_admin"
        )
        assert after.is_system is True
        assert after.resettable is False, "定义行没了就该不可重置——判据是服务端派生的，不是 is_system"
    finally:
        async with engine.begin() as conn:
            # `on conflict do nothing`：若期间有并发同步把出厂默认写回，别把原始失败
            # 掩盖成一条主键冲突（恢复失败会让后面所有测试文件拿不到默认权限）。
            for permission_code in permission_rows:
                await conn.execute(
                    text(
                        "insert into public.rbac_builtin_role_defaults "
                        "(realm, role_code, permission_code) values ('tenant', 'school_admin', :code) "
                        "on conflict do nothing"
                    ),
                    {"code": permission_code},
                )
            for scope_type in scope_rows:
                await conn.execute(
                    text(
                        "insert into public.rbac_builtin_role_scope_defaults "
                        "(realm, role_code, scope_type) values ('tenant', 'school_admin', :scope) "
                        "on conflict do nothing"
                    ),
                    {"scope": scope_type},
                )
        await engine.dispose()


async def test_blank_role_name_is_rejected_by_database_check(stack: None) -> None:
    """TD-198：绕过应用代码的手工 SQL 也写不进一行没名字的角色。"""

    seeded = await _seed("pa-name")
    tenant_id = seeded["tenant_id"]

    engine = make_test_engine("admin")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "insert into public.roles (tenant_id, code, name, is_system, version) "
                    "values (:tid, 'blank_named', '   ', false, 0)"
                ),
                {"tid": str(tenant_id)},
            )
        raise AssertionError("全空白角色名居然插进去了：库层 CHECK 没生效")
    except IntegrityError as exc:
        # 必须钉住**是哪条约束**拦的：只断言 IntegrityError 的话，将来 `roles.code` 的
        # 格式 CHECK 或唯一约束一改，这条用例就变成"因错原因通过"。
        assert "ck_roles_name_not_blank" in str(exc), (
            f"不是角色名 CHECK 拦住的，是别的约束：{exc}"
        )
    finally:
        await engine.dispose()

    # 反证：合法名字必须能插入（否则这条 CHECK 可能是写过头了）
    async with db_session(tenant_id=tenant_id) as session:
        await session.execute(
            text(
                "insert into public.roles (tenant_id, code, name, is_system, version) "
                "values (:tid, 'well_named', '资料管理员', false, 0)"
            ),
            {"tid": str(tenant_id)},
        )
    async with db_session(tenant_id=tenant_id) as session:
        stored = await session.scalar(
            text("select name from public.roles where tenant_id = :tid and code = 'well_named'"),
            {"tid": str(tenant_id)},
        )
    assert str(stored) == "资料管理员"
