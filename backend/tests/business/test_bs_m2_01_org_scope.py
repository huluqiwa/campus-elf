"""业务场景 BS-M2-01：组织结构 + class 数据范围全链路（M2-ORG01-03，HTTP 真实链路）。

Given 校管建班级/课程并录入教师与学生、给教师排课并指派班主任，
When 教师登录后查人员列表、随后被撤销部分任课，
Then 教师的人员可见集合严格等于「本人 ∪ 所任教/管理班级的学生」，撤销立即生效，
    越权访问写 data_scope.denied 审计，组织变更写 org.* 审计（含变更前后值），
    学生只见自己，无任课教师为空集（fail-closed）。
"""

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.auth import revocation
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.main import create_app
from tests.support.auth import login_tenant, seed_tenant_user
from tests.support.db import clean_test_data, database_url, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.business, pytest.mark.integration]


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


async def _seed_principal(school_code: str) -> str:
    """播种校管与内置角色，返回租户 id（供后续真实授权链的授予动作）。

    校管：组织维护 + 人员查看/创建权限，school 范围。

    seed_tenant_user 直建的租户没有内置角色（那是开通函数的活），
    建档绑定角色前必须先 `seed_builtin_roles(tenant_id)` 播种。
    """

    admin_engine = make_test_engine("admin")
    try:
        tenant_id = await seed_tenant_user(
            admin_engine,
            school_code=school_code,
            login_id="m2-principal",
            password="M2-Principal-Pass-1",
            user_type="school_admin",
            permissions=(
                "school.personnel.view",
                "school.personnel.create",
                "school.org.manage",
            ),
            scope_types=("school",),
        )
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(admin_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text("select seed_builtin_roles(:tid)"), {"tid": str(tenant_id[0])}
            )
        return str(tenant_id[0])
    finally:
        await admin_engine.dispose()


async def _org_audit_actions() -> set[str]:
    """管理连接读本批审计动作集合（org.* 与 data_scope.denied）。"""

    async with db_session(platform=True) as session:
        rows = (
            await session.execute(
                text(
                    "select distinct action from audit_logs "
                    "where action like 'org.%' or action = 'data_scope.denied'"
                )
            )
        ).all()
    return {str(row[0]) for row in rows}


async def test_bs_m2_01_teacher_scope_follows_assignments(client: AsyncClient) -> None:
    school_code = f"SCH-M2{uuid4().hex[:6].upper()}"
    school_tenant_id = await _seed_principal(school_code)
    admin = await login_tenant(
        client, school_code=school_code, login_id="m2-principal", password="M2-Principal-Pass-1"
    )

    # ---- Given：3 个班、2 门课；教师 T/T2，学生 S1/S2（班1）、S3（班2） ----
    classes: dict[str, str] = {}
    for name in ("甲班", "乙班", "丙班"):
        response = await client.post(
            "/api/v1/school/classes",
            json={"entry_year": 2026, "grade": "2026级", "name": name},
            headers=admin,
        )
        assert response.status_code == 201, response.text
        classes[name] = response.json()["id"]

    courses: dict[str, str] = {}
    for code, name in (("MATH", "数学"), ("PHY", "物理")):
        response = await client.post(
            "/api/v1/school/courses", json={"code": code, "name": name}, headers=admin
        )
        assert response.status_code == 201, response.text
        courses[code] = response.json()["id"]

    teacher_response = await client.post(
        "/api/v1/school/teachers",
        json={"login_id": "T-1001", "employee_no": "E-1001", "name": "张老师"},
        headers=admin,
    )
    assert teacher_response.status_code == 201, teacher_response.text
    teacher_body = teacher_response.json()
    teacher_id = teacher_body["id"]  # 教师档案 id（任教关系/班主任指派用）
    teacher_user_id = teacher_body["user_id"]  # users.id（人员列表可见集合用）
    teacher_login = teacher_body["login_id"]
    teacher_initial = teacher_body["initial_password"]  # 只出现这一次

    teacher2_response = await client.post(
        "/api/v1/school/teachers",
        json={"login_id": "T-1002", "employee_no": "E-1002", "name": "无课老师"},
        headers=admin,
    )
    assert teacher2_response.status_code == 201
    teacher2_user_id = teacher2_response.json()["user_id"]
    teacher2_initial = teacher2_response.json()["initial_password"]

    students: dict[str, str] = {}
    student_user_ids: dict[str, str] = {}
    student_initials: dict[str, str] = {}
    for student_no, name, class_name in (
        ("S-2001", "学生一", "甲班"),
        ("S-2002", "学生二", "甲班"),
        ("S-2003", "学生三", "乙班"),
    ):
        response = await client.post(
            "/api/v1/school/students",
            json={
                "login_id": student_no,
                "student_no": student_no,
                "name": name,
                "class_id": classes[class_name],
            },
            headers=admin,
        )
        assert response.status_code == 201, response.text
        students[student_no] = response.json()["id"]
        student_user_ids[student_no] = response.json()["user_id"]
        student_initials[student_no] = response.json()["initial_password"]

    # ---- 指派 T 任 甲班数学 + 乙班物理，并任命为甲班班主任（审计含变更前后值） ----
    for class_name, course_code in (("甲班", "MATH"), ("乙班", "PHY")):
        response = await client.post(
            "/api/v1/school/teacher-assignments",
            json={
                "teacher_id": teacher_id,
                "class_id": classes[class_name],
                "course_id": courses[course_code],
                "term": "2026上",
            },
            headers=admin,
        )
        assert response.status_code == 201, response.text

    head_update = await client.put(
        f"/api/v1/school/classes/{classes['甲班']}",
        json={"head_teacher_id": teacher_id},
        headers=admin,
    )
    assert head_update.status_code == 200, head_update.text
    assert head_update.json()["head_teacher_id"] == teacher_id

    # 错误契约：坏游标（CursorError）在消费层映射为 400，不得退化成 500
    bad_cursor = await client.get(
        "/api/v1/school/classes", params={"cursor": "broken-cursor"}, headers=admin
    )
    assert bad_cursor.status_code == 400, bad_cursor.text
    assert bad_cursor.json()["detail"] == "分页游标无效"

    granted_role_code = f"bs_b_view_{uuid4().hex[:8]}"
    # 校管授予 T 的 B 端查看权：矩阵口径 school.personnel.view 对 teacher_b_end 是 △
    # （可授予但出厂不给）。角色分配管理 API 在 M2 后段（TD-137），这里走**真实授权链**
    # 建自定义角色并绑定（roles → role_permissions → user_roles），运行时鉴权不变。
    admin_engine = make_test_engine("admin")
    try:
        from sqlalchemy import text as _text
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(admin_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                _text(
                    "insert into roles (tenant_id, code, name, is_system) "
                    "values (:tenant_id, :code, 'B 端查看（场景授予）', false)"
                ),
                {"tenant_id": school_tenant_id, "code": granted_role_code},
            )
            await session.execute(
                _text(
                    "insert into role_permissions (tenant_id, role_id, permission_id) "
                    "select r.tenant_id, r.id, p.id from roles r, permissions p "
                    "where r.tenant_id = :tenant_id and r.code = :code "
                    "and p.code = 'school.personnel.view'"
                ),
                {"tenant_id": school_tenant_id, "code": granted_role_code},
            )
            await session.execute(
                _text(
                    "insert into user_roles (tenant_id, user_id, role_id) "
                    "select :tenant_id, :user_id, r.id from roles r "
                    "where r.tenant_id = :tenant_id and r.code = :code"
                ),
                {"tenant_id": school_tenant_id, "user_id": teacher_body["user_id"],
                 "code": granted_role_code},
            )
            # T2 同样授予（用于验证「仅 self 范围」的可见性口径）
            await session.execute(
                _text(
                    "insert into user_roles (tenant_id, user_id, role_id) "
                    "select :tenant_id, :user_id, r.id from roles r "
                    "where r.tenant_id = :tenant_id and r.code = :code"
                ),
                {"tenant_id": school_tenant_id, "user_id": teacher2_user_id,
                 "code": granted_role_code},
            )
    finally:
        await admin_engine.dispose()

    # ---- When：T 用初始密码登录（强制改密）后查人员列表 ----
    forced = await login_tenant(
        client, school_code=school_code, login_id=teacher_login, password=teacher_initial
    )
    change = await client.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": teacher_initial,
            "new_password": "T-New-Pass-1001",
            "confirm_password": "T-New-Pass-1001",
        },
        headers=forced,
    )
    assert change.status_code == 200, change.text
    teacher = await login_tenant(
        client, school_code=school_code, login_id=teacher_login, password="T-New-Pass-1001"
    )

    # ---- Then：可见集合 = 本人 ∪ 甲班学生（S1/S2）∪ 乙班学生（S3） ----
    listing = await client.get("/api/v1/school/personnel?limit=100", headers=teacher)
    assert listing.status_code == 200, listing.text
    visible = {item["id"] for item in listing.json()}
    assert visible == {
        teacher_user_id, student_user_ids["S-2001"], student_user_ids["S-2002"],
        student_user_ids["S-2003"],
    }

    # ---- When：撤销乙班任课 → S3 立即不可见（调岗即时生效） ----
    # 先找到乙班的那条任教关系
    assignments = await client.get(
        f"/api/v1/school/teacher-assignments?teacher_id={teacher_id}", headers=admin
    )
    assert assignments.status_code == 200, assignments.text
    target = next(
        a["id"]
        for a in assignments.json()["items"]
        if a["class_id"] == classes["乙班"]
    )
    removed = await client.delete(
        f"/api/v1/school/teacher-assignments/{target}", headers=admin
    )
    assert removed.status_code == 204, removed.text

    listing_after = await client.get("/api/v1/school/personnel?limit=100", headers=teacher)
    assert listing_after.status_code == 200
    visible_after = {item["id"] for item in listing_after.json()}
    assert visible_after == {teacher_user_id, student_user_ids["S-2001"], student_user_ids["S-2002"]}

    # ---- 越权访问：T 直连 S3 的档案 → 404 + data_scope.denied 审计 ----
    denied = await client.get(
        f"/api/v1/school/personnel/{student_user_ids['S-2003']}", headers=teacher
    )
    assert denied.status_code == 404

    # ---- 学生只见自己（首登改密后） ----
    student_forced = await login_tenant(
        client, school_code=school_code, login_id="S-2001", password=student_initials["S-2001"]
    )
    student_change = await client.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": student_initials["S-2001"],
            "new_password": "S-Learn-Pass-7",
            "confirm_password": "S-Learn-Pass-7",
        },
        headers=student_forced,
    )
    assert student_change.status_code == 200, student_change.text
    student = await login_tenant(
        client, school_code=school_code, login_id="S-2001", password="S-Learn-Pass-7"
    )
    # 矩阵 §5 行 173：学生角色不含 personnel.view（—）→ 403（授权 fail-closed）
    student_listing = await client.get("/api/v1/school/personnel?limit=100", headers=student)
    assert student_listing.status_code == 403, student_listing.text

    # ---- 无任课教师：仅 self 范围 → 只见自己一行（「本人」口径） ----
    teacher2_forced = await login_tenant(
        client, school_code=school_code, login_id="T-1002", password=teacher2_initial
    )
    teacher2_change = await client.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": teacher2_initial,
            "new_password": "T2-New-Pass-1002",
            "confirm_password": "T2-New-Pass-1002",
        },
        headers=teacher2_forced,
    )
    assert teacher2_change.status_code == 200, teacher2_change.text
    teacher2 = await login_tenant(
        client, school_code=school_code, login_id="T-1002", password="T2-New-Pass-1002"
    )
    teacher2_listing = await client.get("/api/v1/school/personnel?limit=100", headers=teacher2)
    assert teacher2_listing.status_code == 200, teacher2_listing.text
    assert [item["id"] for item in teacher2_listing.json()] == [teacher2_user_id]

    # ---- cursor 分页（真实接口）：不重不漏 ----
    paged: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        page = await client.get("/api/v1/school/students", params=params, headers=admin)
        assert page.status_code == 200, page.text
        body = page.json()
        paged.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert sorted(paged) == sorted(students.values())

    # ---- 审计断言：组织变更 + 越权审计都落了 ----
    actions = await _org_audit_actions()
    assert {
        "org.class.created",
        "org.class.updated",
        "org.course.created",
        "org.teacher.created",
        "org.student.created",
        "org.assignment.created",
        "org.assignment.deleted",
        "data_scope.denied",
    } <= actions

    # ---- 变更前后值：甲班班主任 updated 审计必须含 before=None/after=teacher_id ----
    async with db_session(platform=True) as session:
        row: Any = (
            await session.execute(
                text(
                    "select metadata_json from audit_logs "
                    "where action = 'org.class.updated' order by sequence desc limit 1"
                )
            )
        ).scalar_one()
    assert row is not None
    assert "before" in row and "after" in row
    # 变更前后值：建班时未指派班主任（before=None），指派后为教师档案 id
    assert row["before"]["head_teacher_id"] is None
    assert row["after"]["head_teacher_id"] == teacher_id
