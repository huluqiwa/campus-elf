"""业务场景 BS-M2-04：身份证加密采集与受控查看（M2 ORG-04，HTTP 真实链路）。

Given 校管建档时可选采集身份证号（D2-A：create/edit + 导入列），
When 同号再录 → 409；非法校验码 → 422；无权限查看 → 403；
    有权限 + 请求体理由 → 明文 + 审计；导入含身份证列 → 受控查看可解密（复审 B1）；
Then 列表只回掩码（复审 M4）；错误行 raw_json 不含明文（复审 B2）；
    KEK 未配置 → 501 且不影响其余建档。

复审回归锚点：B1（AAD 绑定档案行 id，导入写入的密文必须可解）、
B2（raw_json 脱敏）、M1（不存在 → 404 而非 500）、M2（422/501 映射）、
M3（同表唯一索引兜底）、M4（列表掩码经 HTTP 断言）。
"""

import csv
import io
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
from tests.support.auth import login_tenant, seed_tenant_user
from tests.support.db import clean_test_data, database_url, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.business, pytest.mark.integration]

TEST_KEK = "fX2ZZgcEnlYiCBuEvw6FO9WVw4xIbTJCPkIEJOhGhPA="
VALID_ID = "110101199003077758"  # 张老师
VALID_ID_B = "110101199003078611"  # 王老师
VALID_ID_C = "110101199003079331"  # 王老师（导入）
VALID_ID_D = "110101199003085555"  # 钱老师（导入成功行）
INVALID_ID = "110101199003077750"  # 校验码非法
MASK_A = "110***********7758"
MASK_B = "110***********8611"
MASK_C = "110***********9331"
REASON = "医保办理需要查看完整号码"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


def _client_factory(
    monkeypatch: pytest.MonkeyPatch, *, kek: str | None
) -> AsyncIterator[AsyncClient]:
    async def _run() -> AsyncIterator[AsyncClient]:
        admin_engine = make_test_engine("admin")
        try:
            await clean_test_data(admin_engine)
        finally:
            await admin_engine.dispose()

        monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
        monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
        monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
        if kek is None:
            monkeypatch.delenv("ID_CARD_KEK", raising=False)
        else:
            monkeypatch.setenv("ID_CARD_KEK", kek)
        get_settings.cache_clear()
        await revocation.reset_client()

        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

        await revocation.reset_client()
        await dispose_engine()
        get_settings.cache_clear()

    return _run()


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    async for ac in _client_factory(monkeypatch, kek=TEST_KEK):
        yield ac


@pytest.fixture
async def client_no_kek(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    """KEK 缺省（501 门控场景）：其余建档功能必须不受影响。"""

    async for ac in _client_factory(monkeypatch, kek=None):
        yield ac


async def _seed_two_principals(school_code: str) -> str:
    """两个校管：一个有 sensitive.read（主校管），一个没有（普通校管）。"""

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
                "school.personnel.sensitive.read",
            ),
            scope_types=("school",),
        )
        await seed_tenant_user(
            admin_engine,
            school_code=school_code,
            login_id="m2-viewer",
            password="M2-Viewer-Pass-1",
            user_type="school_admin",
            permissions=("school.personnel.view", "school.personnel.create"),
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


def _csv_bytes(rows: list[list[str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode()


async def _upload_and_run(
    client: AsyncClient,
    admin: dict[str, str],
    school_tenant_id: str,
    rows: list[list[str]],
) -> dict[str, Any]:
    upload = await client.post(
        "/api/v1/school/imports",
        files={"file": ("teachers.csv", _csv_bytes(rows), "text/csv")},
        data={"type": "teachers"},
        headers=admin,
    )
    assert upload.status_code == 201, upload.text
    from app.org.import_task import run_import_job

    job_id = upload.json()["id"]
    outcome = await run_import_job({}, job_id, school_tenant_id)
    return {"job_id": job_id, **{k: outcome[k] for k in outcome}}


async def test_bs_m2_04_id_card_lifecycle(client: AsyncClient) -> None:
    school_code = f"SCH-M2C{uuid4().hex[:6].upper()}"
    school_tenant_id = await _seed_two_principals(school_code)
    admin = await login_tenant(
        client, school_code=school_code, login_id="m2-principal", password="M2-Principal-Pass-1"
    )
    viewer = await login_tenant(
        client, school_code=school_code, login_id="m2-viewer", password="M2-Viewer-Pass-1"
    )

    # ---- 建档即采集（D2-A）：POST /teachers 带 id_card_number ----
    teacher_create = await client.post(
        "/api/v1/school/teachers",
        json={
            "login_id": "T-6001",
            "employee_no": "E-6001",
            "name": "张老师",
            "id_card_number": VALID_ID,
        },
        headers=admin,
    )
    assert teacher_create.status_code == 201, teacher_create.text
    teacher_id = teacher_create.json()["id"]  # Teacher.id（编辑/受控查看端点用）

    # ---- 同号第二人建档 → 409（HMAC 查重；唯一索引兜底并发） ----
    duplicate = await client.post(
        "/api/v1/school/teachers",
        json={
            "login_id": "T-6002",
            "employee_no": "E-6002",
            "name": "李老师",
            "id_card_number": VALID_ID,
        },
        headers=admin,
    )
    assert duplicate.status_code == 409, duplicate.text

    # ---- 非法校验码 → 422 ID_CARD_INVALID（复审 M2） ----
    invalid = await client.post(
        "/api/v1/school/teachers",
        json={
            "login_id": "T-6003",
            "employee_no": "E-6003",
            "name": "赵老师",
            "id_card_number": INVALID_ID,
        },
        headers=admin,
    )
    assert invalid.status_code == 422, invalid.text

    # ---- 未采集身份证的教师：受控查看 → 404（复审 M1，随机 id 同样 404） ----
    plain_teacher = await client.post(
        "/api/v1/school/teachers",
        json={"login_id": "T-6004", "employee_no": "E-6004", "name": "孙老师"},
        headers=admin,
    )
    assert plain_teacher.status_code == 201, plain_teacher.text
    no_card = await client.request(
        "GET",
        f"/api/v1/school/personnel/{plain_teacher.json()['id']}/id-card",
        json={"reason": REASON},
        headers=admin,
    )
    assert no_card.status_code == 404, no_card.text
    unknown = await client.request(
        "GET",
        f"/api/v1/school/personnel/{uuid4()}/id-card",
        json={"reason": REASON},
        headers=admin,
    )
    assert unknown.status_code == 404, unknown.text

    # ---- 人员列表只回掩码（复审 M4：经 HTTP 断言，非 DB 直查） ----
    personnel = await client.get("/api/v1/school/personnel?limit=100", headers=admin)
    assert personnel.status_code == 200
    target = next(u for u in personnel.json() if u["display_name"] == "张老师")
    assert target["id_card_mask"] == MASK_A
    assert all(VALID_ID not in str(u) for u in personnel.json())  # 明文不出现在任何列表项

    # ---- 无 sensitive.read → 403 ----
    viewer_read = await client.request(
        "GET",
        f"/api/v1/school/personnel/{teacher_id}/id-card",
        json={"reason": REASON},
        headers=viewer,
    )
    assert viewer_read.status_code == 403, viewer_read.text

    # ---- 有权限 + 请求体理由（D3-A） → 明文 + no-store ----
    full_read = await client.request(
        "GET",
        f"/api/v1/school/personnel/{teacher_id}/id-card",
        json={"reason": REASON},
        headers=admin,
    )
    assert full_read.status_code == 200, full_read.text
    assert full_read.json()["number"] == VALID_ID
    assert full_read.json()["mask"] == MASK_A
    assert full_read.headers.get("cache-control") == "no-store"

    # ---- 理由过短 → 422（D3 ≥10 字符） ----
    short_reason = await client.request(
        "GET",
        f"/api/v1/school/personnel/{teacher_id}/id-card",
        json={"reason": "查一下"},
        headers=admin,
    )
    assert short_reason.status_code == 422, short_reason.text

    # ---- 审计：查看含 reason/掩码；写入动作 created；任何审计元数据不含明文 ----
    async with db_session(platform=True) as session:
        from sqlalchemy import text as _text

        read_audit: Any = (
            await session.execute(
                _text(
                    "select metadata_json from audit_logs "
                    "where action = 'sensitive.id_card.read' "
                    "order by sequence desc limit 1"
                )
            )
        ).scalar_one()
        assert read_audit["reason"] == REASON
        assert read_audit["mask"] == MASK_A

        write_audit: Any = (
            await session.execute(
                _text(
                    "select metadata_json from audit_logs "
                    "where action = 'org.id_card.created' "
                    "order by sequence desc limit 1"
                )
            )
        ).scalar_one()
        assert write_audit["mask"] == MASK_A

        plaintext_audits: int = (
            await session.execute(
                _text("select count(*) from audit_logs where metadata_json::text like :pat"),
                {"pat": f"%{VALID_ID}%"},
            )
        ).scalar_one()
        assert plaintext_audits == 0, "审计元数据泄漏身份证明文"

    # ---- 编辑路径（D2-A）：调岗 + 身份证更新一步请求 ----
    edit = await client.put(
        f"/api/v1/school/teachers/{teacher_id}",
        json={"department_id": None, "id_card_number": VALID_ID_B},
        headers=admin,
    )
    assert edit.status_code == 200, edit.text
    reread = await client.request(
        "GET",
        f"/api/v1/school/personnel/{teacher_id}/id-card",
        json={"reason": REASON},
        headers=admin,
    )
    assert reread.status_code == 200
    assert reread.json()["number"] == VALID_ID_B  # 更新生效且可解密

    # ---- 导入含身份证列（D2-A 导入列） ----
    imported = await _upload_and_run(
        client,
        admin,
        school_tenant_id,
        [["工号", "姓名", "身份证号"], ["E-6005", "王老师", VALID_ID_C]],
    )
    assert imported["success_rows"] == 1, imported

    # 复审 B1 回归：导入写入的密文必须能经受控查看解密（AAD 绑定档案行 id）
    teachers = await client.get("/api/v1/school/teachers?limit=100", headers=admin)
    assert teachers.status_code == 200
    wang = next(t for t in teachers.json()["items"] if t["employee_no"] == "E-6005")
    wang_read = await client.request(
        "GET",
        f"/api/v1/school/personnel/{wang['id']}/id-card",
        json={"reason": REASON},
        headers=admin,
    )
    assert wang_read.status_code == 200, wang_read.text
    assert wang_read.json()["number"] == VALID_ID_C

    # ---- 导入错误行：结构化错误码（复审 M2）+ raw_json 脱敏（复审 B2） ----
    bad_import = await _upload_and_run(
        client,
        admin,
        school_tenant_id,
        [
            ["工号", "姓名", "身份证号"],
            ["E-6006", "钱老师", VALID_ID_D],  # 合法新号 → 成功入库
            ["E-6007", "周老师", VALID_ID_B],  # 与王老师同号 → ID_CARD_DUPLICATE
            ["E-6008", "吴老师", INVALID_ID],  # 校验码非法 → ID_CARD_INVALID
        ],
    )
    # 钱老师行成功；周/吴两行失败
    assert bad_import["success_rows"] == 1, bad_import
    assert bad_import["failed_rows"] == 2, bad_import

    errors = await client.get(
        f"/api/v1/school/imports/{bad_import['job_id']}/errors", headers=admin
    )
    assert errors.status_code == 200, errors.text
    codes = {row["error_code"] for row in errors.json()["items"]}
    assert codes == {"ID_CARD_DUPLICATE", "ID_CARD_INVALID"}, codes

    # raw_json 不含明文（DB 直查：错误行落库值）
    admin_engine = make_test_engine("admin")
    try:
        from sqlalchemy import text as _text2
        from sqlalchemy.ext.asyncio import async_sessionmaker as _asm

        factory = _asm(admin_engine, expire_on_commit=False)
        async with factory() as session:
            raws: list[dict[str, Any]] = list(
                (
                    await session.execute(
                        _text2("select raw_json from import_row_errors where job_id = :jid"),
                        {"jid": bad_import["job_id"]},
                    )
                )
                .scalars()
                .all()
            )
        assert len(raws) == 2
        for raw in raws:
            assert VALID_ID_B not in str(raw), "raw_json 泄漏身份证明文"
            assert INVALID_ID not in str(raw), "raw_json 泄漏非法号原文"
            assert raw["id_card_number"].startswith("110***"), raw
    finally:
        await admin_engine.dispose()


async def test_bs_m2_04_student_id_card(client: AsyncClient) -> None:
    """学生路径（复审 M6）：建档采集 → PUT 编辑更新 → 受控查看；教师↔学生跨表查重。"""

    school_code = f"SCH-M2S{uuid4().hex[:6].upper()}"
    await _seed_two_principals(school_code)
    admin = await login_tenant(
        client, school_code=school_code, login_id="m2-principal", password="M2-Principal-Pass-1"
    )

    student = await client.post(
        "/api/v1/school/students",
        json={
            "login_id": "S-7001",
            "student_no": "S-7001",
            "name": "小明",
            "id_card_number": VALID_ID_C,
        },
        headers=admin,
    )
    assert student.status_code == 201, student.text
    student_id = student.json()["id"]  # Student.id

    # 跨表查重：教师录学生同号 → 409
    cross = await client.post(
        "/api/v1/school/teachers",
        json={
            "login_id": "T-7001",
            "employee_no": "E-7001",
            "name": "陈老师",
            "id_card_number": VALID_ID_C,
        },
        headers=admin,
    )
    assert cross.status_code == 409, cross.text

    # PUT 学生编辑（D2-A）：更新身份证 → 返回掩码
    update = await client.put(
        f"/api/v1/school/students/{student_id}",
        json={"id_card_number": VALID_ID_B},
        headers=admin,
    )
    assert update.status_code == 200, update.text
    assert update.json()["mask"] == MASK_B

    # 受控查看回读：更新后明文为新号（AAD 绑定 Student.id）
    full_read = await client.request(
        "GET",
        f"/api/v1/school/personnel/{student_id}/id-card",
        json={"reason": REASON},
        headers=admin,
    )
    assert full_read.status_code == 200, full_read.text
    assert full_read.json()["number"] == VALID_ID_B

    # 人员列表学生掩码（复审 M4）
    personnel = await client.get("/api/v1/school/personnel?limit=100", headers=admin)
    target = next(u for u in personnel.json() if u["display_name"] == "小明")
    assert target["id_card_mask"] == MASK_B


async def test_bs_m2_04_id_card_not_configured(client_no_kek: AsyncClient) -> None:
    """KEK 未配置（501 门控）：身份证采集 501；不带身份证的建档不受影响。"""

    school_code = f"SCH-M2N{uuid4().hex[:6].upper()}"
    await _seed_two_principals(school_code)
    admin = await login_tenant(
        client_no_kek,
        school_code=school_code,
        login_id="m2-principal",
        password="M2-Principal-Pass-1",
    )

    blocked = await client_no_kek.post(
        "/api/v1/school/teachers",
        json={
            "login_id": "T-8001",
            "employee_no": "E-8001",
            "name": "郑老师",
            "id_card_number": VALID_ID,
        },
        headers=admin,
    )
    assert blocked.status_code == 501, blocked.text

    # 不带身份证字段 → 正常建档（复审登记：501 不阻塞其余功能）
    normal = await client_no_kek.post(
        "/api/v1/school/teachers",
        json={"login_id": "T-8002", "employee_no": "E-8002", "name": "冯老师"},
        headers=admin,
    )
    assert normal.status_code == 201, normal.text

    # 受控查看：目标行无密文 → 404（不是 500/UnboundLocalError，复审 M1）
    no_card = await client_no_kek.request(
        "GET",
        f"/api/v1/school/personnel/{normal.json()['id']}/id-card",
        json={"reason": REASON},
        headers=admin,
    )
    assert no_card.status_code == 404, no_card.text
