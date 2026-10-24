"""业务场景 BS-M2-02：CSV/XLSX 异步导入全链路（M2 ORG-02，HTTP 真实链路 + Arq 任务直调）。

Given 校管上传含「合法行 + 重复学号行 + 缺姓名行」的学生 CSV（conflict=error），
When 上传入队（Arq）并直调任务函数模拟 worker 执行，
Then 进度/汇总可查、错误报告含行号/字段/原因、库内只落合法行（无半成品行）、
    retry 只导失败行且成功、无 import 权限 403、对象存储文件被任务消费。
"""

import os
from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

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
    get_settings.cache_clear()
    await revocation.reset_client()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()


async def _seed_principal(school_code: str) -> str:
    """播种校管 + 内置角色，返回租户 id。"""

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
                "school.personnel.import",
                "school.org.manage",
            ),
            scope_types=("school",),
        )
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(admin_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            from sqlalchemy import text as _text

            await session.execute(
                _text("select seed_builtin_roles(:tid)"), {"tid": str(tenant_id[0])}
            )
        return str(tenant_id[0])
    finally:
        await admin_engine.dispose()


def _student_csv(rows: list[tuple[str, str, str, str, str]]) -> bytes:
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["学号", "姓名", "性别", "年级", "班级"])
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


async def test_bs_m2_02_import_errors_and_retry(client: AsyncClient) -> None:
    school_code = f"SCH-M2I{uuid4().hex[:6].upper()}"
    tenant_id = await _seed_principal(school_code)
    admin = await login_tenant(
        client, school_code=school_code, login_id="m2-principal", password="M2-Principal-Pass-1"
    )

    # ---- Given：预先建一个班（2026级/1班）供导入定位 ----
    class_create = await client.post(
        "/api/v1/school/classes", json={"grade": "2026级", "name": "1班"}, headers=admin
    )
    assert class_create.status_code == 201, class_create.text

    # ---- When：上传含「合法 ×2 + 重复学号 ×1 + 缺姓名 ×1」的学生 CSV ----
    csv_data = _student_csv(
        [
            ("S-3001", "学生甲", "male", "2026级", "1班"),
            ("S-3002", "学生乙", "female", "2026级", "1班"),
            ("S-3001", "重复学号", "male", "2026级", "1班"),
            ("S-3004", "", "female", "2026级", "1班"),
            ("S-3005", "定位失败", "male", "2099级", "不存在的班"),
        ]
    )
    upload = await client.post(
        "/api/v1/school/imports",
        files={"file": ("students.csv", csv_data, "text/csv")},
        data={"type": "students", "conflict": "error"},
        headers=admin,
    )
    assert upload.status_code == 201, upload.text
    job_id = upload.json()["id"]

    # ---- Then：任务执行（直调模拟 worker）→ completed_with_errors，计数正确 ----
    outcome = await run_import_job({}, job_id, tenant_id)
    assert outcome["status"] == "completed_with_errors", outcome
    assert (outcome["total_rows"], outcome["success_rows"], outcome["failed_rows"]) == (5, 2, 3), outcome

    detail = await client.get(f"/api/v1/school/imports/{job_id}", headers=admin)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["status"] == "completed_with_errors"
    assert body["total_rows"] == 5 and body["success_rows"] == 2 and body["failed_rows"] == 3

    # ---- 错误报告：行号/字段/原因齐全，含重复与缺姓名与班级定位失败 ----
    errors = await client.get(f"/api/v1/school/imports/{job_id}/errors", headers=admin)
    assert errors.status_code == 200, errors.text
    error_items = errors.json()["items"]
    assert {item["row_no"] for item in error_items} == {3, 4, 5}
    codes = {item["error_code"] for item in error_items}
    # 行 3 是文件内重复学号（D3：FILE_DUPLICATE，不受 conflict 影响）
    assert codes == {"FILE_DUPLICATE", "VALUE_REQUIRED", "CLASS_NOT_FOUND"}
    duplicate = next(item for item in error_items if item["row_no"] == 3)
    assert duplicate["error_code"] == "FILE_DUPLICATE"
    assert duplicate["field"] == "login_id"

    # ---- 库内只落合法行（无半成品）——经 personnel 接口断言 user 数 ----
    personnel = await client.get("/api/v1/school/personnel?limit=100", headers=admin)
    assert personnel.status_code == 200
    student_rows = [u for u in personnel.json() if u["user_type"] == "student"]
    assert {u["display_name"] for u in student_rows} == {"学生甲", "学生乙"}

    # ---- When：retry 失败行（修正不了重复/缺姓名，但班级定位行改不了——验证 retry 语义） ----
    retry = await client.post(f"/api/v1/school/imports/{job_id}/retry", headers=admin)
    assert retry.status_code == 201, retry.text
    derived_job_id = retry.json()["job_id"]

    outcome2 = await run_import_job({}, derived_job_id, tenant_id)
    assert outcome2["success_rows"] == 0 and outcome2["failed_rows"] == 3

    # retry 后 row 3 的重复从「文件内重复」变为「库内重复」：ORG_LOGIN_TAKEN 且 field=login_id
    errors2 = await client.get(f"/api/v1/school/imports/{derived_job_id}/errors", headers=admin)
    assert errors2.status_code == 200, errors2.text
    dup2 = next(item for item in errors2.json()["items"] if item["row_no"] == 3)
    assert dup2["error_code"] == "ORG_LOGIN_TAKEN"
    assert dup2["field"] == "login_id"

    # 源 job 已指向派生；再次 retry 原 job → 409
    retry_again = await client.post(f"/api/v1/school/imports/{job_id}/retry", headers=admin)
    assert retry_again.status_code == 409

    # ---- 审计：建档走真实链路（org.student.created ×2 来自导入） ----
    async with db_session(platform=True) as session:
        count: int = (
            await session.execute(
                text(
                    "select count(*) from audit_logs where action = 'org.student.created' "
                    "and metadata_json->>'student_no' in ('S-3001','S-3002')"
                )
            )
        ).scalar_one()
    assert count == 2


async def test_bs_m2_02_import_requires_permission(client: AsyncClient) -> None:
    """无 import 权限的账号上传 → 403（矩阵 §5 行 174：仅超管/管理员/导入码持有者）。"""

    school_code = f"SCH-M2I{uuid4().hex[:6].upper()}"
    await _seed_principal(school_code)
    # 教师账号（无 school.personnel.import）
    admin = await login_tenant(
        client, school_code=school_code, login_id="m2-principal", password="M2-Principal-Pass-1"
    )
    teacher_create = await client.post(
        "/api/v1/school/teachers",
        json={"login_id": "T-NOIMP", "employee_no": "E-NOIMP", "name": "无权教师"},
        headers=admin,
    )
    assert teacher_create.status_code == 201, teacher_create.text
    teacher = await login_tenant(
        client, school_code=school_code,
        login_id="T-NOIMP", password=teacher_create.json()["initial_password"],
    )
    change = await client.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": teacher_create.json()["initial_password"],
            "new_password": "B-End-Pw-777",
            "confirm_password": "B-End-Pw-777",
        },
        headers=teacher,
    )
    assert change.status_code == 200

    # 改密会撤销改密前的全部会话 → 用新口令重新登录后再验证权限拒绝
    teacher = await login_tenant(
        client, school_code=school_code, login_id='T-NOIMP', password='B-End-Pw-777'
    )

    denied = await client.post(
        "/api/v1/school/imports",
        files={"file": ("students.csv", _student_csv([]), "text/csv")},
        data={"type": "students"},
        headers=teacher,
    )
    assert denied.status_code == 403, denied.text


@pytest.mark.timeout(900)  # 1000 行建档 bcrypt(cost12) ≈ 8 分钟（远超默认 120s）
async def test_bs_m2_02_import_thousand_rows(client: AsyncClient) -> None:
    """验收：1000 行异步导入 → 进度/结果可查，全部成功（性能观感基线）。"""

    school_code = f"SCH-M2I{uuid4().hex[:6].upper()}"
    tenant_id = await _seed_principal(school_code)
    admin = await login_tenant(
        client, school_code=school_code, login_id="m2-principal", password="M2-Principal-Pass-1"
    )
    class_create = await client.post(
        "/api/v1/school/classes", json={"grade": "2026级", "name": "大班"}, headers=admin
    )
    assert class_create.status_code == 201

    rows = [(f"S-{i:05d}", f"学生{i:04d}", "male", "2026级", "大班") for i in range(1, 1001)]
    upload = await client.post(
        "/api/v1/school/imports",
        files={"file": ("students-1000.csv", _student_csv(rows), "text/csv")},
        data={"type": "students"},
        headers=admin,
    )
    assert upload.status_code == 201, upload.text
    job_id = upload.json()["id"]

    outcome = await run_import_job({}, job_id, tenant_id)
    assert outcome["status"] == "completed", outcome
    assert (outcome["total_rows"], outcome["success_rows"], outcome["failed_rows"]) == (
        1000, 1000, 0), outcome

    detail = await client.get(f"/api/v1/school/imports/{job_id}", headers=admin)
    assert detail.json()["total_rows"] == 1000
