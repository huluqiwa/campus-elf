"""业务场景 BS-M2-06：入学年份与学年流转（M2 ORG-06，HTTP 真实链路）。

Given 学校已有三届班级（2024级=高二、2025级=高一）且班内有学生/任教/班主任指派，
When 夏季开学流转（dry_run 预览 → 执行），
Then 当前年级批量刷新、毕业届归档（默认列表退出、显式包含可见）；
    学生/任教/班主任指派/班级范围行逐项不变；重复幂等键 409；
    留级=学生转班到新届；入学年份不可改；跨届同名班可建、同届同名 409。
"""

import os
from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

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
        pytest.skip("TEST_DATABASE_URL 未设置")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def client(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
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


async def _seed_principals(school_code: str) -> str:
    admin_engine = make_test_engine("admin")
    try:
        tenant_id = await seed_tenant_user(
            admin_engine,
            school_code=school_code,
            login_id="m6-admin",
            password="M6-Admin-Pass-1",
            user_type="school_admin",
            permissions=(
                "school.personnel.view",
                "school.personnel.create",
                "school.personnel.edit",
                "school.org.manage",
            ),
            scope_types=("school",),
        )
        await seed_tenant_user(
            admin_engine,
            school_code=school_code,
            login_id="m6-viewer",
            password="M6-Viewer-Pass-1",
            user_type="school_admin",
            permissions=("school.personnel.view",),
            scope_types=("school",),
        )
        from sqlalchemy import text as _text
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(admin_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                _text("select seed_builtin_roles(:tid)"), {"tid": str(tenant_id[0])}
            )
        return str(tenant_id[0])
    finally:
        await admin_engine.dispose()


def _cls(entry_year: int, grade: str, name: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {"entry_year": entry_year, "grade": grade, "name": name}
    payload.update(extra)
    return payload


async def test_bs_m2_06_academic_year_rollover(client: AsyncClient) -> None:
    school_code = f"SCH-M6A{uuid4().hex[:6].upper()}"
    school_tenant_id = await _seed_principals(school_code)
    admin = await login_tenant(
        client, school_code=school_code, login_id="m6-admin", password="M6-Admin-Pass-1"
    )
    viewer = await login_tenant(
        client, school_code=school_code, login_id="m6-viewer", password="M6-Viewer-Pass-1"
    )

    # ---- 建三届班级：2024级1班(高二)、2025级1班/2班(高一)；跨届同名可建 ----
    created: dict[str, dict[str, object]] = {}
    for entry_year, grade, name in (
        (2024, "高二", "1班"),
        (2025, "高一", "1班"),
        (2025, "高一", "2班"),
    ):
        r = await client.post(
            "/api/v1/school/classes", json=_cls(entry_year, grade, name), headers=admin
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["entry_year"] == entry_year and body["status"] == "active"
        created[f"{entry_year}-{name}"] = body
    class_2024 = created["2024-1班"]
    class_2025_1 = created["2025-1班"]

    # ---- 同届同名 → 409 ----
    dup = await client.post("/api/v1/school/classes", json=_cls(2025, "高一", "1班"), headers=admin)
    assert dup.status_code == 409, dup.text

    # ---- 入学年份不可改（PUT 携带 entry_year → 422） ----
    immutable = await client.put(
        f"/api/v1/school/classes/{class_2025_1['id']}",
        json={"entry_year": 2027},
        headers=admin,
    )
    assert immutable.status_code == 422, immutable.text

    # ---- 人员与关系（不变量锚点）：班主任（2024级1班）、任教、学生（2025级1班） ----
    teacher = await client.post(
        "/api/v1/school/teachers",
        json={"login_id": "T-9001", "employee_no": "E-9001", "name": "王老师"},
        headers=admin,
    )
    assert teacher.status_code == 201, teacher.text
    teacher_id = teacher.json()["id"]

    head = await client.put(
        f"/api/v1/school/classes/{class_2024['id']}",
        json={"head_teacher_id": teacher_id},
        headers=admin,
    )
    assert head.status_code == 200, head.text

    course = await client.post(
        "/api/v1/school/courses", json={"code": "MATH", "name": "数学"}, headers=admin
    )
    assert course.status_code == 201, course.text
    assignment = await client.post(
        "/api/v1/school/teacher-assignments",
        json={
            "teacher_id": teacher_id,
            "class_id": class_2025_1["id"],
            "course_id": course.json()["id"],
            "term": "2025秋",
        },
        headers=admin,
    )
    assert assignment.status_code == 201, assignment.text

    student = await client.post(
        "/api/v1/school/students",
        json={
            "login_id": "S-9001",
            "student_no": "S-9001",
            "name": "小明",
            "class_id": class_2025_1["id"],
        },
        headers=admin,
    )
    assert student.status_code == 201, student.text
    student_id = student.json()["id"]

    async def _scope_row_count() -> int:
        admin_engine = make_test_engine("admin")
        try:
            from sqlalchemy import text as _t
            from sqlalchemy.ext.asyncio import async_sessionmaker as _a

            factory = _a(admin_engine, expire_on_commit=False)
            async with factory() as session:
                return (
                    await session.execute(
                        _t("select count(*) from data_scopes where tenant_id = :tid"),
                        {"tid": school_tenant_id},
                    )
                ).scalar_one()
        finally:
            await admin_engine.dispose()

    scope_rows_before = await _scope_row_count()

    # ---- 无 school.org.manage → 403 ----
    denied = await client.post(
        "/api/v1/school/classes/rollover",
        json={
            "advances": [{"from_grade": "高一", "to_grade": "高二"}],
            "archive_grades": ["高二"],
            "idempotency_key": "rollover-2026-x",
        },
        headers=viewer,
    )
    assert denied.status_code == 403, denied.text

    # ---- dry_run：预览影响清单（含入学年份），不写库 ----
    payload = {
        "advances": [{"from_grade": "高一", "to_grade": "高二"}],
        "archive_grades": ["高二"],
        "idempotency_key": "rollover-2026-summer",
    }
    preview = await client.post(
        "/api/v1/school/classes/rollover", json={**payload, "dry_run": True}, headers=admin
    )
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body["dry_run"] is True
    assert {(c["entry_year"], c["name"], c["to_grade"]) for c in preview_body["advanced"]} == {
        (2025, "1班", "高二"),
        (2025, "2班", "高二"),
    }
    assert [(c["entry_year"], c["name"]) for c in preview_body["archived"]] == [(2024, "1班")]

    listing = await client.get("/api/v1/school/classes", headers=admin)
    grades_now = {c["name"]: c["grade"] for c in listing.json()["items"] if c["entry_year"] == 2025}
    assert grades_now == {"1班": "高一", "2班": "高一"}  # dry_run 未写库

    # ---- 执行：批量刷新 + 归档 ----
    done = await client.post(
        "/api/v1/school/classes/rollover", json={**payload, "dry_run": False}, headers=admin
    )
    assert done.status_code == 200, done.text
    done_body = done.json()
    assert done_body["dry_run"] is False and done_body["rollover_id"]
    assert done_body["advanced_count"] == 2 and done_body["archived_count"] == 1

    # 当前年级刷新 + 归档态
    listing = await client.get("/api/v1/school/classes", headers=admin)
    items = listing.json()["items"]
    active_2025 = {c["name"]: c["grade"] for c in items if c["entry_year"] == 2025}
    assert active_2025 == {"1班": "高二", "2班": "高二"}
    assert all(c["id"] != class_2024["id"] for c in items)  # 归档届退出默认列表

    archived_list = await client.get(
        "/api/v1/school/classes", params={"include_archived": "true"}, headers=admin
    )
    archived_row = next(c for c in archived_list.json()["items"] if c["id"] == class_2024["id"])
    assert archived_row["status"] == "archived" and archived_row["grade"] == "高二"

    # ---- 不变量：学生仍在原班、任教关系仍在、范围行数不变 ----
    students = await client.get("/api/v1/school/students?limit=100", headers=admin)
    student_row = next(s for s in students.json()["items"] if s["id"] == student_id)
    assert student_row["class_id"] == class_2025_1["id"]

    assignments = await client.get("/api/v1/school/teacher-assignments?limit=100", headers=admin)
    assert any(a["class_id"] == class_2025_1["id"] for a in assignments.json()["items"])

    assert await _scope_row_count() == scope_rows_before

    # ---- 重复幂等键 → 409 ----
    replay = await client.post(
        "/api/v1/school/classes/rollover", json={**payload, "dry_run": False}, headers=admin
    )
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"] == "该幂等键已执行过学年流转，请勿重复提交"

    # ---- 流转审计：单条含映射/计数/操作者 ----
    from sqlalchemy import text as _text

    async with db_session(platform=True) as session:
        audit_row = (
            await session.execute(
                _text(
                    "select metadata_json, actor_id from audit_logs "
                    "where action = 'org.academic_year.rollover' "
                    "order by sequence desc limit 1"
                )
            )
        ).one()
        assert audit_row.metadata_json["idempotency_key"] == "rollover-2026-summer"
        assert audit_row.metadata_json["advanced_count"] == 2
        assert audit_row.metadata_json["archived_count"] == 1
        assert len(audit_row.metadata_json["advanced_ids"]) == 2

    # ---- 建新届 + 留级：学生转班到新届高一班 ----
    new_cohort = await client.post(
        "/api/v1/school/classes", json=_cls(2026, "高一", "1班"), headers=admin
    )
    assert new_cohort.status_code == 201, new_cohort.text
    new_class_id = new_cohort.json()["id"]

    transfer = await client.post(
        f"/api/v1/school/students/{student_id}/transfer",
        json={"class_id": new_class_id},
        headers=admin,
    )
    assert transfer.status_code == 200, transfer.text
    assert transfer.json()["class_id"] == new_class_id

    # 转入归档班 → 422
    to_archived = await client.post(
        f"/api/v1/school/students/{student_id}/transfer",
        json={"class_id": class_2024["id"]},
        headers=admin,
    )
    assert to_archived.status_code == 422, to_archived.text
    assert to_archived.json()["detail"] == "目标班级已归档（毕业届），不可转入"

    # ---- 归档态封闭（复审 Major 1）：建学生/排课/删除/改历史年级均被拒 ----
    archived_student = await client.post(
        "/api/v1/school/students",
        json={
            "login_id": "S-9002",
            "student_no": "S-9002",
            "name": "小刚",
            "class_id": class_2024["id"],
        },
        headers=admin,
    )
    assert archived_student.status_code == 422, archived_student.text
    assert archived_student.json()["detail"] == "目标班级已归档（毕业届），不可写入"

    archived_assignment = await client.post(
        "/api/v1/school/teacher-assignments",
        json={
            "teacher_id": teacher_id,
            "class_id": class_2024["id"],
            "course_id": course.json()["id"],
            "term": "2026春",
        },
        headers=admin,
    )
    assert archived_assignment.status_code == 422, archived_assignment.text

    delete_archived = await client.delete(
        f"/api/v1/school/classes/{class_2024['id']}", headers=admin
    )
    assert delete_archived.status_code == 409, delete_archived.text
    assert delete_archived.json()["detail"] == "已归档班级是历史沿革记录，不可删除"

    rewrite_history = await client.put(
        f"/api/v1/school/classes/{class_2024['id']}",
        json={"grade": "高三"},
        headers=admin,
    )
    assert rewrite_history.status_code == 422, rewrite_history.text

    # 班主任/辅导员档案纠错仍允许：换一位教师接任归档班的班主任 → 200
    teacher2 = await client.post(
        "/api/v1/school/teachers",
        json={"login_id": "T-9002", "employee_no": "E-9002", "name": "陈老师"},
        headers=admin,
    )
    assert teacher2.status_code == 201, teacher2.text
    correction = await client.put(
        f"/api/v1/school/classes/{class_2024['id']}",
        json={"head_teacher_id": teacher2.json()["id"]},
        headers=admin,
    )
    assert correction.status_code == 200, correction.text
    assert correction.json()["head_teacher_id"] == teacher2.json()["id"]


async def test_bs_m2_06_rollover_rollback_on_failure(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """中途失败（审计写入注入故障）→ 整体回滚：班级与批次行零残留（复审 Minor 4）。"""

    school_code = f"SCH-M6B{uuid4().hex[:6].upper()}"
    await _seed_principals(school_code)
    admin = await login_tenant(
        client, school_code=school_code, login_id="m6-admin", password="M6-Admin-Pass-1"
    )
    cls = await client.post("/api/v1/school/classes", json=_cls(2025, "高一", "1班"), headers=admin)
    assert cls.status_code == 201, cls.text

    from app.org import academic_year as ay

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("审计写入注入故障")

    monkeypatch.setattr(ay, "_audit", _boom)
    with pytest.raises(RuntimeError, match="审计写入注入故障"):
        await client.post(
            "/api/v1/school/classes/rollover",
            json={
                "advances": [{"from_grade": "高一", "to_grade": "高二"}],
                "idempotency_key": "rollover-rollback-1",
                "dry_run": False,
            },
            headers=admin,
        )

    # 零残留：当前年级未变、批次表无行
    listing = await client.get("/api/v1/school/classes", headers=admin)
    assert listing.json()["items"][0]["grade"] == "高一"

    admin_engine = make_test_engine("admin")
    try:
        from sqlalchemy import text as _t
        from sqlalchemy.ext.asyncio import async_sessionmaker as _a

        factory = _a(admin_engine, expire_on_commit=False)
        async with factory() as session:
            count: int = (
                await session.execute(_t("select count(*) from academic_year_rollovers"))
            ).scalar_one()
        assert count == 0
    finally:
        await admin_engine.dispose()
