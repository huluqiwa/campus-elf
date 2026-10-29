"""业务场景 BS-M2UI3-F：权限账号页与绑定页的**前置契约**（TD-187g 决策 1、2）。

Given 一所学校：一名校方超管、一名只持 `school.permission_account.manage` 的"权限管理员"、
      一名教师、一名学生，以及另一所学校的一名教师；
When 权限管理员打开「权限账号」页（`GET /school/permission-accounts`）与
      「绑定角色」页（`GET /school/bindable-roles`）——
Then 账号列表里出现的是**本班的教师与校方管理员**：学生不出现、外校账号不出现、
      已删除的账号不出现；每行带**已绑角色码**与 `roles_version`（绑定页要拿它做 CAS），
      **不回显登录名**（学号/工号属敏感标识符）；
And  绑定页的角色清单里**没有超管角色**（列出来 = 能勾不能存），也**没有权限码清单**
      （完整矩阵是 `school.role.manage` 的事，只持账号管理权的人不该顺手拿到）；
And  没有 `school.permission_account.manage` 的人点这两个页面 → **403**，且审计里留有
      这一次拒绝（`auth.permission.denied`，主体是他自己、资源是那个权限码）；
And  翻页游标被篡改 → **400**，不是"悄悄回到第一页"。

为什么单独一条业务场景：这两条端点是前端 TD-187g-B / TD-187g-C 的地基。
服务层口径（过滤/分页/形状）由 `tests/integration/test_rbac_permission_accounts.py` 钉，
这里钉的是**HTTP 这一层的真实结果**：谁能进、进了看到什么、看不到时有没有留痕。
"""

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth import revocation
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.main import create_app
from tests.support.auth import login_tenant, seed_tenant_user
from tests.support.db import clean_test_data, database_url, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.business, pytest.mark.integration]

SUPER_LOGIN = "perm-acct-super"
SUPER_PASSWORD = "PermAcct-Super-Pass-1"
ADMIN_LOGIN = "perm-acct-admin"
ADMIN_PASSWORD = "PermAcct-Admin-Pass-1"
TEACHER_LOGIN = "perm-acct-teacher"
TEACHER_PASSWORD = "PermAcct-Teacher-Pass-1"
STUDENT_LOGIN = "perm-acct-student"
STUDENT_PASSWORD = "PermAcct-Student-Pass-1"
PLAIN_LOGIN = "perm-acct-plain"
PLAIN_PASSWORD = "PermAcct-Plain-Pass-1"

ACCOUNT_MANAGER_CODE = "school.permission_account.manage"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def client(migrated_database: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
    finally:
        await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    get_settings.cache_clear()
    await revocation.reset_client()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()


async def _seed_school(school_code: str) -> dict[str, Any]:
    """建一所学校：超管 + 权限管理员 + 教师 + 学生，并给教师绑上教师角色。

    权限管理员只持 `school.permission_account.manage`（直建角色，不走授权链路：
    授权链路由 `test_bs_m2ui2_rbac_admin.py` 覆盖），用来证明"账号管理权"与
    "角色配置权"是两条独立的权限码。
    """

    engine = make_test_engine("admin")
    try:
        tenant_id, super_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=SUPER_LOGIN,
            password=SUPER_PASSWORD,
            user_type="school_admin",
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text("select seed_builtin_roles(:tid)"), {"tid": str(tenant_id)}
            )
            await session.execute(
                text(
                    "insert into user_roles (tenant_id, user_id, role_id) "
                    "select :tid, :uid, r.id from roles r "
                    "where r.tenant_id = :tid and r.code = 'school_super_admin'"
                ),
                {"tid": str(tenant_id), "uid": str(super_id)},
            )
        _, admin_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=ADMIN_LOGIN,
            password=ADMIN_PASSWORD,
            user_type="school_admin",
            permissions=(ACCOUNT_MANAGER_CODE,),
        )
        _, teacher_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=TEACHER_LOGIN,
            password=TEACHER_PASSWORD,
            user_type="teacher",
        )
        _, student_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=STUDENT_LOGIN,
            password=STUDENT_PASSWORD,
            user_type="student",
        )
        # 没有 `school.permission_account.manage` 的人：用来钉 403
        _, plain_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=PLAIN_LOGIN,
            password=PLAIN_PASSWORD,
            user_type="school_admin",
            permissions=("school.dashboard.view",),
        )
        # 另一所学校的人：跨租不得出现
        foreign_tenant_id, foreign_id = await seed_tenant_user(
            engine,
            school_code=f"{school_code}X",
            login_id=f"perm-acct-foreign-{school_code}",
            password=PLAIN_PASSWORD,
            user_type="teacher",
        )
        async with factory() as session, session.begin():
            await session.execute(
                text("select seed_builtin_roles(:tid)"), {"tid": str(foreign_tenant_id)}
            )
        # 给教师绑一个角色：列表里那行要显示得出"这个人已经有什么角色"
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "insert into user_roles (tenant_id, user_id, role_id) "
                    "select :tid, :uid, r.id from roles r "
                    "where r.tenant_id = :tid and r.code = 'teacher_b_end'"
                ),
                {"tid": str(tenant_id), "uid": str(teacher_id)},
            )
    finally:
        await engine.dispose()

    return {
        "tenant_id": tenant_id,
        "super_id": super_id,
        "admin_id": admin_id,
        "teacher_id": teacher_id,
        "student_id": student_id,
        "plain_id": plain_id,
        "foreign_id": foreign_id,
    }


async def _audit_denials(tenant_id: Any, code: str) -> list[dict[str, Any]]:
    """本租的 `auth.permission.denied` 审计（按 tenant_id 过滤，挡跨租同名行）。"""

    async with db_session(platform=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select actor_id, resource_id, result, metadata_json from audit_logs "
                        "where action = 'auth.permission.denied' and tenant_id = :tid "
                        "order by sequence"
                    ),
                    {"tid": str(tenant_id)},
                )
            )
            .mappings()
            .all()
        )
    return [
        {
            "actor_id": None if row["actor_id"] is None else str(row["actor_id"]),
            "resource_id": None if row["resource_id"] is None else str(row["resource_id"]),
            "result": str(row["result"]),
            "metadata": dict(row["metadata_json"] or {}),
        }
        for row in rows
        if str(row["resource_id"]) == code
    ]


async def test_permission_accounts_page_lists_only_bindable_accounts_of_this_school(
    client: AsyncClient,
) -> None:
    """BS-M2UI3-F：账号列表 = 本班"能被绑角色"的账号，带已绑角色与版本，不回显登录名。"""

    school_code = f"SCH-PA{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    headers = await login_tenant(
        client, school_code=school_code, login_id=ADMIN_LOGIN, password=ADMIN_PASSWORD
    )

    response = await client.get("/api/v1/school/permission-accounts", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    items = body["items"]
    ids = {str(item["id"]) for item in items}

    assert str(seeded["admin_id"]) in ids, "持账号管理权的人自己就在列表里"
    assert str(seeded["teacher_id"]) in ids, "教师是可绑角色的账号，必须列"
    assert str(seeded["super_id"]) in ids, "校方管理员也是可绑角色的账号"
    assert str(seeded["student_id"]) not in ids, "学生是 C 端账号，不在这条权限的射程内"
    assert str(seeded["foreign_id"]) not in ids, "别的学校的人不得出现"

    teacher = next(item for item in items if str(item["id"]) == str(seeded["teacher_id"]))
    assert teacher["user_type"] == "teacher"
    assert teacher["status"] == "active"
    assert teacher["role_codes"] == ["teacher_b_end"], "绑定页要显示这个人已经有什么角色"
    assert teacher["roles_version"] == 0, "没改过绑定的账号 `roles_version` 是 0（CAS 起点）"
    assert teacher["is_super_admin"] is False
    assert "login_id" not in teacher, "学号/工号属敏感标识符，不回显"

    # 超管账号出现在列表里，但服务端要把它标出来：它的角色绑定由平台开通流程管理，
    # 租户内改绑必然 403 `BINDING_SUPER_ADMIN_FORBIDDEN`，前端据此关掉绑定入口（TD-242）。
    super_account = next(item for item in items if str(item["id"]) == str(seeded["super_id"]))
    assert super_account["is_super_admin"] is True, "持有超管角色的账号必须被标出来"

    assert body["next_cursor"] is None, "账号不够一页时不该给下一页游标"


async def test_bindable_roles_catalog_omits_super_admin_and_permission_codes(
    client: AsyncClient,
) -> None:
    """BS-M2UI3-F：绑定页的角色清单 = 能选的（不含超管）+ 不含权限码。"""

    school_code = f"SCH-PB{uuid4().hex[:6].upper()}"
    await _seed_school(school_code)
    headers = await login_tenant(
        client, school_code=school_code, login_id=ADMIN_LOGIN, password=ADMIN_PASSWORD
    )

    response = await client.get("/api/v1/school/bindable-roles", headers=headers)
    assert response.status_code == 200, response.text
    items = response.json()
    codes = {item["code"] for item in items}

    assert "school_super_admin" not in codes, "超管角色不可经绑定接口授出，列出来=能勾不能存"
    assert "school_admin" in codes
    assert "teacher_b_end" in codes
    for item in items:
        assert "permission_codes" not in item, (
            "绑定目录带上了权限码清单：只持账号管理权的人不该拿到完整权限矩阵"
        )
        assert set(item) == {"id", "code", "name", "is_system"}, f"字段集变了：{sorted(item)}"


async def test_both_endpoints_deny_callers_without_the_permission_code(
    client: AsyncClient,
) -> None:
    """BS-M2UI3-F：没有账号管理权的人点这两个页面 → 403，且拒绝留痕。"""

    school_code = f"SCH-PC{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    headers = await login_tenant(
        client, school_code=school_code, login_id=PLAIN_LOGIN, password=PLAIN_PASSWORD
    )

    for path in ("/api/v1/school/permission-accounts", "/api/v1/school/bindable-roles"):
        response = await client.get(path, headers=headers)
        assert response.status_code == 403, f"{path} 期望 403，实际 {response.status_code}"
        assert ACCOUNT_MANAGER_CODE in str(response.json()["detail"])

    denials = await _audit_denials(seeded["tenant_id"], ACCOUNT_MANAGER_CODE)
    assert len(denials) == 2, f"两次拒绝各留一条痕，实际 {len(denials)} 条"
    assert all(item["actor_id"] == str(seeded["plain_id"]) for item in denials), (
        "拒绝审计的主体是发起请求的那个人（不是超管、不是空）"
    )
    assert all(item["result"] == "failure" for item in denials)
    paths = {str(item["metadata"].get("path")) for item in denials}
    assert paths == {
        "/api/v1/school/permission-accounts",
        "/api/v1/school/bindable-roles",
    }, f"审计里要能看出被挡的是哪条接口：{paths}"


async def test_permission_accounts_page_rejects_a_tampered_cursor(client: AsyncClient) -> None:
    """BS-M2UI3-F：游标被篡改 → 400，绝不悄悄回到第一页（否则翻页看起来"没反应"）。"""

    school_code = f"SCH-PD{uuid4().hex[:6].upper()}"
    await _seed_school(school_code)
    headers = await login_tenant(
        client, school_code=school_code, login_id=ADMIN_LOGIN, password=ADMIN_PASSWORD
    )

    response = await client.get(
        "/api/v1/school/permission-accounts",
        params={"cursor": "not-a-valid-cursor"},
        headers=headers,
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "分页游标无效"
