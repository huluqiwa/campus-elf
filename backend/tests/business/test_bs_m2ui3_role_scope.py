"""业务场景 BS-M2UI3-C：角色级数据范围编辑改的是**真实可见行集**（TD-187c，简报 §13）。

Given 一所学校：超管（绑内置 `school_super_admin`）、keeper（只绑自定义角色 `roster_keeper`，
      权限 `school.personnel.view`，**一条范围行都没有**）、1 名教师 + 3 名学生（真实档案），
When 超管把 `roster_keeper` 的范围依次替换为 `school` → `self` → 空清单，
Then keeper **同一条 token** 的 `/school/personnel` 行集随之变为「全校 → 仅本人 → 空集」。
    这条是本功能点的业务事实：范围编辑不是版本号自增加一条审计，而是数据可见范围的真实收放；
    预览与执行同形状，`affected_users` 是真实绑定人数，审计记 added/removed/resulting。

再钉三件只有走 HTTP 才成立的事（服务层用例证不到接口契约）：
- 手工提交 `class`/`course` → 人读 `{code,message}` 的 422（payload 有意不用 `Literal` 收窄，
  Pydantic 先拦就会变成数组形 422），且范围行一字未动、版本没推高，并留 `*.scopes.denied`；
- 未知类型排在可编辑性判定**之前**报 `UNKNOWN_SCOPE_TYPE`，且这类入参非法**不留安全审计**；
- keeper 给自己所在角色加 `school`（触达本人 + 净增）→ 未确认 422、能力面没有变大；
  确认 + 理由后 200，专项审计先 `denied` 后 `success` 且 `kind=scope`。

反向对照：超管改**别人**所在角色的范围 = 日常配置，既不要求确认，也不产生一条自扩权审计。
"""

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID, uuid4

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

ADMIN_LOGIN = "ScopeBs-Admin"
KEEPER_LOGIN = "ScopeBs-Keeper"
ADMIN_PASSWORD = "ScopeBs-Admin-Pass-1"
KEEPER_PASSWORD = "ScopeBs-Keeper-Pass-1"

ROLE_CODE = "roster_keeper"
EDIT_REASON = "把花名册可见范围放到全校（业务场景 BS-M2UI3-C）"
SELF_GRANT_REASON = "本人代管全校花名册核对"


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


# --- 播种与观测辅助（观测一律直查库，不用接口的返回当证据） ---


async def _seed_school(school_code: str) -> dict[str, Any]:
    """租户 + 内置角色 + 超管 + 一名尚未绑角色的 keeper（返回三个内部 id）。

    keeper 不给任何用户级范围与权限：可见集合初始为空集（解析层 fail-closed），
    于是"范围编辑生效"只能表现为行集真的变了，而不是本来就看得见。
    """

    engine = make_test_engine("admin")
    try:
        tenant_id, admin_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=ADMIN_LOGIN,
            password=ADMIN_PASSWORD,
            user_type="school_admin",
            scope_types=("school",),
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
                {"tid": str(tenant_id), "uid": str(admin_id)},
            )
        keeper_tenant, keeper_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=KEEPER_LOGIN,
            password=KEEPER_PASSWORD,
            user_type="school_admin",
        )
        assert keeper_tenant == tenant_id, "同 school_code 必须复用同一租户"
    finally:
        await engine.dispose()
    return {"tenant_id": tenant_id, "admin_id": admin_id, "keeper_id": keeper_id}


async def _build_org(client: AsyncClient, admin: dict[str, str]) -> dict[str, Any]:
    """真实建出一个班 + 1 名教师 + 3 名学生（人员列表的可见行集要有内容可数）。"""

    response = await client.post(
        "/api/v1/school/classes",
        json={"entry_year": 2026, "grade": "2026级", "name": "甲班"},
        headers=admin,
    )
    assert response.status_code == 201, response.text
    class_id = response.json()["id"]

    teacher = await client.post(
        "/api/v1/school/teachers",
        json={"login_id": "T-3001", "employee_no": "E-3001", "name": "范围老师"},
        headers=admin,
    )
    assert teacher.status_code == 201, teacher.text
    student_ids: list[str] = []
    for no in ("S-3001", "S-3002", "S-3003"):
        created = await client.post(
            "/api/v1/school/students",
            json={"login_id": no, "student_no": no, "name": f"学生{no}", "class_id": class_id},
            headers=admin,
        )
        assert created.status_code == 201, created.text
        student_ids.append(created.json()["user_id"])
    return {"teacher_user_id": teacher.json()["user_id"], "student_user_ids": student_ids}


async def _create_role(
    client: AsyncClient, admin: dict[str, str], *, permission_codes: list[str]
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/school/roles",
        json={"code": ROLE_CODE, "name": "花名册保管员", "permission_codes": permission_codes},
        headers=admin,
    )
    assert response.status_code == 201, response.text
    role: dict[str, Any] = response.json()
    assert role["code"] == ROLE_CODE
    return role


async def _bind_keeper(tenant_id: UUID, keeper_id: UUID, role_id: UUID) -> None:
    """角色→用户的绑定：绑定接口是 TD-187d，这里按既有口径直插关系表。"""

    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text("insert into user_roles (tenant_id, user_id, role_id) values (:t, :u, :r)"),
                {"t": str(tenant_id), "u": str(keeper_id), "r": str(role_id)},
            )
    finally:
        await engine.dispose()


async def _visible_personnel_ids(client: AsyncClient, headers: dict[str, str]) -> set[str]:
    response = await client.get("/api/v1/school/personnel?limit=100", headers=headers)
    assert response.status_code == 200, response.text
    return {str(item["id"]) for item in response.json()}


async def _tenant_user_ids(tenant_id: UUID) -> set[str]:
    """全校可见的应然集合：`school` 范围下人员列表 = 本校全部用户行（仓储没有额外状态过滤）。"""

    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows: list[Any] = list(
                (
                    await session.execute(
                        text("select id from users where tenant_id = :tid"), {"tid": str(tenant_id)}
                    )
                )
                .scalars()
                .all()
            )
            return {str(user_id) for user_id in rows}
    finally:
        await engine.dispose()


async def _role_scope_types(tenant_id: UUID, role_id: UUID) -> set[str]:
    async with db_session(tenant_id=tenant_id) as session:
        rows: list[Any] = list(
            (
                await session.execute(
                    text(
                        "select scope_type from data_scopes "
                        "where tenant_id = :tid and role_id = :rid"
                    ),
                    {"tid": tenant_id, "rid": role_id},
                )
            )
            .scalars()
            .all()
        )
        return {str(item) for item in rows}


async def _role_version(tenant_id: UUID, role_id: UUID) -> int:
    async with db_session(tenant_id=tenant_id) as session:
        value = await session.scalar(
            text("select version from roles where tenant_id = :tid and id = :rid"),
            {"tid": tenant_id, "rid": role_id},
        )
    return int(value)


async def _audits(action: str) -> list[dict[str, Any]]:
    """按写入顺序取回 `result / resource_id / metadata_json`（审计是业务结果，不是日志装饰）。"""

    async with db_session(platform=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select result, resource_id, metadata_json from audit_logs "
                        "where action = :action order by sequence"
                    ),
                    {"action": action},
                )
            )
            .mappings()
            .all()
        )
    return [
        {
            "result": str(row["result"]),
            "resource_id": None if row["resource_id"] is None else str(row["resource_id"]),
            "metadata": dict(row["metadata_json"] or {}),
        }
        for row in rows
    ]


async def test_bs_m2ui3c_role_scope_edit_changes_what_the_holder_sees(
    client: AsyncClient,
) -> None:
    school_code = f"SCH-C{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    tenant_id: UUID = seeded["tenant_id"]
    keeper_id: UUID = seeded["keeper_id"]
    admin = await login_tenant(
        client, school_code=school_code, login_id=ADMIN_LOGIN, password=ADMIN_PASSWORD
    )
    org = await _build_org(client, admin)
    role = await _create_role(client, admin, permission_codes=["school.personnel.view"])
    role_id = UUID(role["id"])
    await _bind_keeper(tenant_id, keeper_id, role_id)

    keeper = await login_tenant(
        client, school_code=school_code, login_id=KEEPER_LOGIN, password=KEEPER_PASSWORD
    )

    # ---- Given：有查看权、没有任何范围行 → 空集（fail-closed），范围才是这道闸门 ----
    assert await _visible_personnel_ids(client, keeper) == set()

    # ---- When：预览全校范围 ----
    preview = await client.post(
        f"/api/v1/school/roles/{role_id}/scopes/preview",
        json={"scope_types": ["school"]},
        headers=admin,
    )
    assert preview.status_code == 200, preview.text
    previewed = preview.json()
    assert previewed.pop("version") == role["version"]
    assert previewed == {
        "added": ["school"],
        "removed": [],
        "affected_users": 1,
        "empties_scope": False,
        "retained_non_editable": [],
        "newly_reaching_self": [],
        "requires_confirmation": False,
    }

    # ---- Then：执行后 keeper 同一条 token 立即看到全校（不重新登录、不换令牌） ----
    saved = await client.put(
        f"/api/v1/school/roles/{role_id}/scopes",
        json={"scope_types": ["school"], "version": await _role_version(tenant_id, role_id)},
        headers=admin,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == role["version"] + 1
    assert await _role_scope_types(tenant_id, role_id) == {"school"}

    visible = await _visible_personnel_ids(client, keeper)
    assert visible == await _tenant_user_ids(tenant_id)
    assert {org["teacher_user_id"], *org["student_user_ids"]} <= visible

    updated = await _audits("school.role.scopes.updated")
    assert len(updated) == 1, updated
    assert updated[0]["resource_id"] == str(role_id)
    assert updated[0]["metadata"] == {
        "role_code": ROLE_CODE,
        "added": ["school"],
        "removed": [],
        "resulting_scopes": ["school"],
        "affected_users": 1,
        "reason": None,
    }
    # 超管改别人所在角色的范围 = 日常配置：既没要求确认，也不该多出一条自扩权审计
    assert await _audits("school.role.self_grant") == []

    # ---- When：收回到 self → 只剩本人；再到空清单 → 空集 ----
    narrowed = await client.put(
        f"/api/v1/school/roles/{role_id}/scopes",
        json={"scope_types": ["self"], "version": await _role_version(tenant_id, role_id)},
        headers=admin,
    )
    assert narrowed.status_code == 200, narrowed.text
    assert await _role_scope_types(tenant_id, role_id) == {"self"}
    assert await _visible_personnel_ids(client, keeper) == {str(keeper_id)}

    emptied_preview = await client.post(
        f"/api/v1/school/roles/{role_id}/scopes/preview",
        json={"scope_types": []},
        headers=admin,
    )
    assert emptied_preview.status_code == 200, emptied_preview.text
    assert emptied_preview.json()["empties_scope"] is True

    emptied = await client.put(
        f"/api/v1/school/roles/{role_id}/scopes",
        json={
            "scope_types": [],
            "version": await _role_version(tenant_id, role_id),
            "reason": EDIT_REASON,
        },
        headers=admin,
    )
    assert emptied.status_code == 200, emptied.text
    assert await _role_scope_types(tenant_id, role_id) == set()
    assert await _visible_personnel_ids(client, keeper) == set()

    audits = await _audits("school.role.scopes.updated")
    assert [entry["metadata"]["resulting_scopes"] for entry in audits] == [
        ["school"],
        ["self"],
        [],
    ], audits
    assert audits[1]["metadata"]["added"] == ["self"]
    assert audits[1]["metadata"]["removed"] == ["school"]
    assert audits[2]["metadata"] == {
        "role_code": ROLE_CODE,
        "added": [],
        "removed": ["self"],
        "resulting_scopes": [],
        "affected_users": 1,
        "reason": EDIT_REASON,
    }


@pytest.mark.parametrize("submitted", ["class", "course"])
async def test_bs_m2ui3c_non_editable_scope_types_are_rejected_with_an_audit(
    client: AsyncClient, submitted: str
) -> None:
    """`class`/`course` 必须带具体 id（本端点没那个入参通道）、`course` 本期还不生效：
    手工授必须 422 + 留痕，且库里一字不改。

    422 必须是服务层的 `{code,message}` 形状——payload 有意不用 `Literal` 收窄类型，
    否则 FastAPI 的数组形校验错误会抢在这个契约前面。
    """

    school_code = f"SCH-C{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    tenant_id: UUID = seeded["tenant_id"]
    admin = await login_tenant(
        client, school_code=school_code, login_id=ADMIN_LOGIN, password=ADMIN_PASSWORD
    )
    role = await _create_role(client, admin, permission_codes=["school.personnel.view"])
    role_id = UUID(role["id"])
    version_before = await _role_version(tenant_id, role_id)

    response = await client.put(
        f"/api/v1/school/roles/{role_id}/scopes",
        json={"scope_types": [submitted], "version": version_before, "reason": EDIT_REASON},
        headers=admin,
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "SCOPE_TYPE_NOT_EDITABLE", detail
    assert submitted in detail["message"], detail
    assert await _role_scope_types(tenant_id, role_id) == set()
    assert await _role_version(tenant_id, role_id) == version_before, "被拒不得推高版本"

    denied = await _audits("school.role.scopes.denied")
    assert [(e["result"], e["metadata"]["reason"]) for e in denied] == [
        ("denied", "SCOPE_TYPE_NOT_EDITABLE")
    ]
    # 判据要可复盘：留下被探测的清单，才分得清"手滑勾错"和"反复试探"
    assert denied[0]["metadata"]["requested_scope_types"] == [submitted]

    # 类型族两档在预览里同码（免得运营看到"预览说能过、保存被拒"），但预览被拒不落安全审计
    preview = await client.post(
        f"/api/v1/school/roles/{role_id}/scopes/preview",
        json={"scope_types": [submitted]},
        headers=admin,
    )
    assert preview.status_code == 422, preview.text
    assert preview.json()["detail"]["code"] == "SCOPE_TYPE_NOT_EDITABLE"
    assert len(await _audits("school.role.scopes.denied")) == 1


async def test_bs_m2ui3c_unknown_scope_type_is_reported_before_editability(client: AsyncClient) -> None:
    """`{'bogus','class'}` 报 `UNKNOWN_SCOPE_TYPE` 而不是 `SCOPE_TYPE_NOT_EDITABLE`。

    可编辑性要先"认识"这个类型才谈得上判定；顺序反了会把拼错的字面量算成一次"越权试探"。
    入参非法按四类分档**不留安全审计**（与 `UNKNOWN_PERMISSION_CODE` 同口径，TD-201）。
    """

    school_code = f"SCH-C{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    tenant_id: UUID = seeded["tenant_id"]
    admin = await login_tenant(
        client, school_code=school_code, login_id=ADMIN_LOGIN, password=ADMIN_PASSWORD
    )
    role = await _create_role(client, admin, permission_codes=["school.personnel.view"])
    role_id = UUID(role["id"])

    response = await client.put(
        f"/api/v1/school/roles/{role_id}/scopes",
        json={"scope_types": ["bogus", "class"], "version": 0, "reason": EDIT_REASON},
        headers=admin,
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "UNKNOWN_SCOPE_TYPE"
    assert await _role_scope_types(tenant_id, role_id) == set()
    assert await _audits("school.role.scopes.denied") == []


async def test_bs_m2ui3c_scope_upgrade_on_own_role_needs_explicit_ack(client: AsyncClient) -> None:
    """keeper 给自己所在角色加 `school`：未确认 422 且能力面没有变大；确认后 200 并两条专项审计。

    与权限侧同一条判据，只是换了个宇宙（范围类型 vs 权限码）：`school` = 全校可见。
    超管在创建角色时把 `school.role.manage` 一起写进去（转授禁令只挡普通主体），
    所以这条链上的操作者是**普通管理员**，会真的被打断。
    """

    school_code = f"SCH-C{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    tenant_id: UUID = seeded["tenant_id"]
    keeper_id: UUID = seeded["keeper_id"]
    admin = await login_tenant(
        client, school_code=school_code, login_id=ADMIN_LOGIN, password=ADMIN_PASSWORD
    )
    role = await _create_role(
        client, admin, permission_codes=["school.personnel.view", "school.role.manage"]
    )
    role_id = UUID(role["id"])
    await _bind_keeper(tenant_id, keeper_id, role_id)

    keeper = await login_tenant(
        client, school_code=school_code, login_id=KEEPER_LOGIN, password=KEEPER_PASSWORD
    )
    assert await _visible_personnel_ids(client, keeper) == set()

    unacked = await client.put(
        f"/api/v1/school/roles/{role_id}/scopes",
        json={"scope_types": ["school"], "version": await _role_version(tenant_id, role_id)},
        headers=keeper,
    )
    assert unacked.status_code == 422, unacked.text
    assert unacked.json()["detail"]["code"] == "SELF_GRANT_CONFIRMATION_REQUIRED"
    assert await _role_scope_types(tenant_id, role_id) == set()
    assert await _role_version(tenant_id, role_id) == 0, "被拒不得推高版本"
    assert await _visible_personnel_ids(client, keeper) == set(), "打断必须真的挡住扩权"

    grants = await _audits("school.role.self_grant")
    assert [(e["result"], e["metadata"]["kind"], e["metadata"]["newly_reaching_self"]) for e in grants] == [
        ("denied", "scope", ["school"])
    ], grants
    assert len(await _audits("school.role.scopes.denied")) == 1

    acked = await client.put(
        f"/api/v1/school/roles/{role_id}/scopes",
        json={
            "scope_types": ["school"],
            "version": await _role_version(tenant_id, role_id),
            "reason": SELF_GRANT_REASON,
            "self_grant_ack": True,
        },
        headers=keeper,
    )
    assert acked.status_code == 200, acked.text
    assert await _visible_personnel_ids(client, keeper) == await _tenant_user_ids(tenant_id)

    grants = await _audits("school.role.self_grant")
    assert [e["result"] for e in grants] == ["denied", "success"], grants
    entry = grants[-1]["metadata"]
    assert entry["kind"] == "scope"
    assert entry["newly_reaching_self"] == ["school"]
    assert entry["confirmed_by"] == "self_grant_ack"
    assert entry["reason"] == SELF_GRANT_REASON
