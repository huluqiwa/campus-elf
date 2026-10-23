"""M1-E2 业务场景：审批职责分离与平台跨租只读的真实链路（spec §3.4）。

Given/When/Then 全部走 HTTP + 真实基础设施（PG/对象存储/MailHog），断言的是**业务结果**：
租户是否真的开通了、审核记录里例外标记是否真的落下、审计里是否真的能读出理由、
跨租读到的行是否真的只属于那所学校并且已经脱敏。

- 自批被拒不是"接口返回 403"就完了：申请必须仍可被他人正常审核（没有半笔开通残留）；
- 例外开关是运营策略：走平台接口改，改完**下一次审批**即生效（不重启、不踢会话）；
- 跨租通道默认拒绝：没有权限码就没有读取，也没有"看过"的痕迹。
"""

import os
import shutil
from collections.abc import AsyncIterator, Iterator
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.auth import revocation
from app.config import get_settings
from app.db.session import dispose_engine
from app.main import create_app
from app.settings.store import SELF_EXCEPTION_KEY
from app.storage.objects import build_object_store
from tests.support.auth import (
    TEST_PLATFORM_PASSWORD,
    login_platform,
    login_tenant,
    seed_platform_user,
    seed_tenant_user,
)
from tests.support.db import (
    clean_test_data,
    database_url,
    make_session_factory,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn

pytestmark = pytest.mark.business

PDF_BYTES = b"%PDF-1.4\n% synthetic qualification\n%%EOF\n"
SCHOOL_CODE = "SCH-BS-E2"
VIEW = "school.personnel.view"

SUBMITTER = "e2-submitter"
OPERATOR = "e2-operator"
NO_READER = "e2-no-reader"
SCHOOL_ADMIN = "E2-ADMIN"
TEACHER = "E2-TEACHER"
TENANT_PASSWORD = "Scope-Test-Pass-1"


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
async def env(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """清空业务数据、种平台账号，并把应用指向测试栈（对象存储 + MailHog）。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
        await seed_platform_user(
            admin_engine,
            account=SUBMITTER,
            permissions=("platform.tenant.review", "platform.config.edit"),
        )
        await seed_platform_user(
            admin_engine,
            account=OPERATOR,
            permissions=("platform.cross_tenant.read",),
        )
        await seed_platform_user(
            admin_engine, account=NO_READER, permissions=("platform.tenant.view",)
        )
    finally:
        await admin_engine.dispose()

    object_dir = Path(os.environ["TEST_OBJECT_STORE_DIR"]) / uuid4().hex
    object_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("OBJECT_STORE_BACKEND", "local")
    monkeypatch.setenv("OBJECT_STORE_DIR", str(object_dir))
    monkeypatch.setenv("SMTP_HOST", os.environ["TEST_SMTP_HOST"])
    monkeypatch.setenv("SMTP_PORT", os.environ["TEST_SMTP_PORT"])
    monkeypatch.setenv("SMTP_FROM", "no-reply@campus-elf.test")
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    monkeypatch.delenv("DEV_SKIP_SCHOOL_REVIEW", raising=False)
    get_settings.cache_clear()
    # 每个用例一个新事件循环：上一个循环里创建的 Redis 客户端必须丢掉，否则第二次登录
    # 会拿着已关闭的 loop 的连接池写会话键（表现为 `Event loop is closed`）。
    await revocation.reset_client()

    yield

    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()
    shutil.rmtree(object_dir, ignore_errors=True)


@pytest.fixture
async def client(env: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _platform_user_id(account: str) -> UUID:
    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            user_id = await session.scalar(
                text("select id from platform_users where account = :account"),
                {"account": account},
            )
    finally:
        await engine.dispose()
    assert user_id is not None
    return UUID(str(user_id))


async def _seed_request(*, email: str, submitter_id: UUID) -> UUID:
    """造出一笔"平台代提交"的待审申请（提交人列有值，才谈得上自批）。"""

    registration_id = uuid4()
    object_key = f"onboarding/{registration_id}/qualification.pdf"
    await build_object_store(get_settings()).put(object_key, PDF_BYTES, "application/pdf")
    engine = make_test_engine("admin")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "insert into tenant_registration_requests ("
                    "id, school_name, address, leadership, email, "
                    "qualification_object_key, qualification_sha256, qualification_size, "
                    "qualification_mime, status, submitted_at, submitted_by_platform_user_id"
                    ") values ("
                    ":id, '业务场景中学', '示范路 1 号', '张校长', :email, :object_key, "
                    ":sha256, :size, 'application/pdf', 'pending', now(), :submitter)"
                ),
                {
                    "id": registration_id,
                    "email": email,
                    "object_key": object_key,
                    "sha256": sha256(PDF_BYTES).hexdigest(),
                    "size": len(PDF_BYTES),
                    "submitter": submitter_id,
                },
            )
    finally:
        await engine.dispose()
    return registration_id


async def _scalar(sql: str, **params: object) -> object:
    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            return (await session.execute(text(sql), params)).scalar()
    finally:
        await engine.dispose()


async def _audit_metadata(action: str) -> list[object]:
    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            rows: list[object] = list(
                (
                    await session.execute(
                        text("select metadata_json from audit_logs where action = :a order by sequence"),
                        {"a": action},
                    )
                ).scalars()
            )
            return rows
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bs_self_approval_denied_then_exception_granted_and_tenant_live(
    client: AsyncClient,
) -> None:
    """Given 提交人想审自己代提交的申请；When 直接被拒 → 运营开例外 → 再审；Then 真实开通且留证据。"""

    submitter_id = await _platform_user_id(SUBMITTER)
    headers = await login_platform(client, SUBMITTER, TEST_PLATFORM_PASSWORD)
    registration_id = await _seed_request(
        email="principal@bs-e2.test", submitter_id=submitter_id
    )
    review_url = f"/api/v1/platform/tenant-requests/{registration_id}/review"

    # When 1：例外未开启，自批被拒
    denied = await client.post(review_url, json={"result": "approved"}, headers=headers)
    assert denied.status_code == 403, denied.text

    # Then 1：申请仍在待审队列里，没有半笔开通残留
    assert await _scalar(
        "select status from tenant_registration_requests where id = :id", id=registration_id
    ) == "pending"
    assert await _scalar(
        "select count(*) from school_review_records where request_id = :id", id=registration_id
    ) == 0
    assert await _scalar("select count(*) from tenants") == 0
    denial_rows = await _audit_metadata("approval.self_approval.denied")
    assert len(denial_rows) == 1

    # When 2：运营通过平台接口开启例外（带理由），提交人再审一次（也带理由）
    opened = await client.put(
        f"/api/v1/platform/settings/{SELF_EXCEPTION_KEY}",
        json={"value": True, "reason": "唯一在岗审核人出差前自批这一笔（工单 AUD-31）"},
        headers=headers,
    )
    assert opened.status_code == 200, opened.text

    approved = await client.post(
        review_url,
        json={"result": "approved", "reason": "在岗唯一审核人，工单 AUD-31"},
        headers=headers,
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "approved" and body["email_sent"] is True

    # Then 2：租户真的在线、审核记录带例外标记与提交人、用掉例外的审计能读出理由
    tenant_id = body["tenant_id"]
    assert await _scalar(
        "select status from tenants where id = :id", id=tenant_id
    ) == "active"
    record = await _scalar(
        "select submitted_by_platform_user_id::text || '/' || self_exception_used::text "
        "from school_review_records where request_id = :id",
        id=registration_id,
    )
    assert str(record) == f"{submitter_id}/true"
    used = await _audit_metadata("approval.self_exception.used")
    assert len(used) == 1
    assert isinstance(used[0], dict)
    assert "AUD-31" in used[0]["reason"]


@pytest.mark.asyncio
async def test_bs_cross_tenant_read_default_denies_and_records_reason(
    client: AsyncClient,
) -> None:
    """Given 两所学校的在册人员；When 平台侧跨租只读；Then 有权限+理由才读得到，且只读那一所、邮箱已脱敏。"""

    admin_engine = make_test_engine("admin")
    try:
        for login_id, user_type, scope_types in (
            (SCHOOL_ADMIN, "school_admin", ("school",)),
            (TEACHER, "teacher", ("self",)),
        ):
            await seed_tenant_user(
                admin_engine,
                school_code=SCHOOL_CODE,
                login_id=login_id,
                password=TENANT_PASSWORD,
                user_type=user_type,
                email=f"{login_id.lower()}@bs-e2.test",
                permissions=(VIEW,),
                scope_types=scope_types,
            )
    finally:
        await admin_engine.dispose()

    tenant_id = UUID(
        str(
            await _scalar(
                "select id from tenants where school_code = :code", code=SCHOOL_CODE
            )
        )
    )
    read_url = "/api/v1/platform/cross-tenant/personnel/read"

    # When/Then 1（先证明默认拒绝）：没有 `platform.cross_tenant.read` 的平台账号 → 403，且没有读取痕迹
    plain = await login_platform(client, NO_READER, TEST_PLATFORM_PASSWORD)
    refused = await client.post(read_url, json={"tenant_id": str(tenant_id), "reason": "想看看"}, headers=plain)
    assert refused.status_code == 403, refused.text
    assert await _scalar(
        "select count(*) from audit_logs where action = 'platform.cross_tenant.read'"
    ) == 0

    # When 2：有权限的运营带理由读取
    reader = await login_platform(client, OPERATOR, TEST_PLATFORM_PASSWORD)
    without_reason = await client.post(read_url, json={"tenant_id": str(tenant_id)}, headers=reader)
    assert without_reason.status_code == 422, without_reason.text

    response = await client.post(
        read_url,
        json={"tenant_id": str(tenant_id), "reason": "教育局要求的在册教师清点（工单 AUD-33）"},
        headers=reader,
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    expected_ids = {
        str(
            await _scalar(
                "select u.id from users u join tenants t on t.id = u.tenant_id"
                " where t.school_code = :code and u.login_id = :login_id",
                code=SCHOOL_CODE,
                login_id=login_id,
            )
        )
        for login_id in (SCHOOL_ADMIN, TEACHER)
    }
    assert {str(item["id"]) for item in items} == expected_ids
    assert all("***" in (item["email_masked"] or "***") for item in items)
    # 与校方侧同一脱敏形状：跨租通道也不外发学号/工号（口径 ⑥，名册不可整体导出）
    assert "login_id" not in response.text

    audits = await _audit_metadata("platform.cross_tenant.read")
    assert len(audits) == 1
    assert isinstance(audits[0], dict)
    assert "AUD-33" in audits[0]["reason"]
    assert audits[0]["row_count"] == len(items)

    # Then 3：同一理由的读取仍然受租户边界约束——校方侧的 self 范围用户只看得到自己
    school_headers = await login_tenant(
        client, school_code=SCHOOL_CODE, login_id=TEACHER, password=TENANT_PASSWORD
    )
    listed = await client.get("/api/v1/school/personnel", headers=school_headers)
    assert listed.status_code == 200, listed.text
    teacher_id = await _scalar(
        "select u.id from users u join tenants t on t.id = u.tenant_id"
        " where t.school_code = :code and u.login_id = :login_id",
        code=SCHOOL_CODE,
        login_id=TEACHER,
    )
    assert [str(item["id"]) for item in listed.json()] == [str(teacher_id)]
