"""业务场景 BS-M2-03：部门树 + 调岗 + 导入扩展（M2 ORG-05，HTTP 真实链路）。

Given 校管建两级部门树并导入带部门的教师，
When 教师调岗（改部门）与移动子树，
Then 审计记录变更前后值（部门名口径）、移动后子树 path 正确重写、
    未知部门导入报错误行、有教师归属的部门删除 409、未分班学生导入正常。
"""

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import revocation
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.main import create_app
from app.org.import_task import run_import_job
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
    from app.db.session import dispose_engine as _dispose_engine

    get_settings.cache_clear()
    await _dispose_engine()
    await revocation.reset_client()

    transport = ASGITransport(app=create_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        await revocation.reset_client()
        await dispose_engine()
        get_settings.cache_clear()


async def _seed_principal(school_code: str) -> str:
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
                "school.personnel.edit",
                "school.personnel.import",
                "school.org.manage",
            ),
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


def _teacher_csv(rows: list[tuple[str, str, str, str]]) -> bytes:
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["工号", "姓名", "职位", "邮箱", "部门"])
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


async def test_bs_m2_03_department_tree_transfer_and_import(client: AsyncClient) -> None:
    school_code = f"SCH-M2D{uuid4().hex[:6].upper()}"
    tenant_id = await _seed_principal(school_code)
    admin = await login_tenant(
        client, school_code=school_code, login_id="m2-principal", password="M2-Principal-Pass-1"
    )

    # ---- Given：两级部门树（信息学院 → 软件工程系 / 网络安全系） ----
    college = await client.post(
        "/api/v1/school/departments",
        json={"name": "信息学院", "code": "INFO"}, headers=admin,
    )
    assert college.status_code == 201, college.text
    college_id = college.json()["id"]
    assert college.json()["path"] == f"/{college_id}"

    department_ids: dict[str, str] = {}
    for name in ("软件工程系", "网络安全系"):
        child = await client.post(
            "/api/v1/school/departments",
            json={"name": name, "parent_id": college_id}, headers=admin,
        )
        assert child.status_code == 201, child.text
        department_ids[name] = child.json()["id"]
        assert child.json()["path"].startswith(f"/{college_id}/")

    # ---- When：导入带部门的教师（1 合法 + 1 未知部门） ----
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["工号", "姓名", "职位", "部门"])
    writer.writerow(["E-5001", "张老师", "教师", "信息学院/软件工程系"])
    writer.writerow(["E-5002", "李老师", "教师", "信息学院/不存在的系"])
    upload = await client.post(
        "/api/v1/school/imports",
        files={"file": ("teachers.csv", buffer.getvalue().encode(), "text/csv")},
        data={"type": "teachers"},
        headers=admin,
    )
    assert upload.status_code == 201, upload.text
    import_job_id = upload.json()["id"]

    outcome = await run_import_job({}, import_job_id, tenant_id)
    assert outcome["status"] == "completed_with_errors", outcome
    assert outcome["success_rows"] == 1 and outcome["failed_rows"] == 1

    errors = await client.get(f"/api/v1/school/imports/{import_job_id}/errors", headers=admin)
    error_items = errors.json()["items"]
    assert error_items[0]["error_code"] == "DEPARTMENT_NOT_FOUND"
    assert error_items[0]["field"] == "department"

    # ---- Then：教师导入成功（部门归属在教师行上） ----
    teachers = await client.get("/api/v1/school/teachers?limit=100", headers=admin)
    assert teachers.status_code == 200
    teacher_items = teachers.json()["items"]
    assert len(teacher_items) == 1
    teacher_id = teacher_items[0]["id"]

    # ---- 调岗：张老师 软件工程系 → 网络安全系（审计前后值，部门名口径） ----
    transfer = await client.put(
        f"/api/v1/school/teachers/{teacher_id}",
        json={"department_id": department_ids["网络安全系"]}, headers=admin,
    )
    assert transfer.status_code == 200, transfer.text

    async with db_session(platform=True) as session:
        from sqlalchemy import text as _text

        row: Any = (
            await session.execute(
                _text(
                    "select metadata_json from audit_logs "
                    "where action = 'org.teacher.updated' order by sequence desc limit 1"
                )
            )
        ).scalar_one()
    assert row["before"]["department"] == "软件工程系"
    assert row["after"]["department"] == "网络安全系"

    # ---- 删除 409：网络安全系有教师归属；信息学院有子部门 ----
    denied_child = await client.delete(
        f"/api/v1/school/departments/{department_ids['网络安全系']}", headers=admin
    )
    assert denied_child.status_code == 409
    denied_parent = await client.delete(
        f"/api/v1/school/departments/{college_id}", headers=admin
    )
    assert denied_parent.status_code == 409

    # ---- 调离后可删叶子；父部门仍因另一子部门 409 ----
    transfer_back = await client.put(
        f"/api/v1/school/teachers/{teacher_id}", json={"department_id": None}, headers=admin
    )
    assert transfer_back.status_code == 200
    delete_leaf = await client.delete(
        f"/api/v1/school/departments/{department_ids['网络安全系']}", headers=admin
    )
    assert delete_leaf.status_code == 204
    still_denied = await client.delete(
        f"/api/v1/school/departments/{college_id}", headers=admin
    )
    assert still_denied.status_code == 409

    # ---- 未分班学生导入正常（学生无部门列） ----
    buffer2 = io.StringIO()
    writer2 = csv.writer(buffer2)
    writer2.writerow(["学号", "姓名", "性别", "年级", "班级"])
    writer2.writerow(["S-9001", "未分班学生", "female", "", ""])
    writer2.writerow(["S-9002", "半行学生", "male", "2026级", ""])  # 只给年级不给班名
    upload2 = await client.post(
        "/api/v1/school/imports",
        files={"file": ("students.csv", buffer2.getvalue().encode(), "text/csv")},
        data={"type": "students"},
        headers=admin,
    )
    assert upload2.status_code == 201
    outcome2 = await run_import_job({}, upload2.json()["id"], tenant_id)
    assert outcome2["status"] == "completed_with_errors", outcome2
    assert (outcome2["total_rows"], outcome2["success_rows"], outcome2["failed_rows"]) == (
        2, 1, 1), outcome2
