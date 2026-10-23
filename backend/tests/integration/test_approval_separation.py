"""M1-E2 集成测试：审批职责分离（RBAC-03）。

口径（`docs/design-briefs/M1-E2-data-scope.md` §0 ③⑦⑧、§2-D）：

- 提交人不能审批自己提交的注册申请：**应用层**先判（干净的 403/422 + 拒绝审计），
  数据库函数 `review_registration_request()` 再判（挡住绕过接口层的内部调用），
  `school_review_records` 上的同表 `CHECK` 兜住手工 SQL；
- 例外开关默认关闭，开启要理由 + 审计，并且**每请求从库读**：改完下一次请求即生效；
- 同人审批必须带例外标记，不同人审批照常通过（正反两面取证，证明约束不是空转）。
"""

import os
import shutil
from collections.abc import AsyncIterator, Iterator
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db.models.onboarding import SchoolReviewRecord, TenantRegistrationRequest
from app.db.session import db_session, dispose_engine
from app.main import create_app
from app.onboarding.service import (
    OnboardingError,
    _call_review,
    approve_request,
    reject_request,
)
from app.settings import store
from app.storage.objects import build_object_store
from tests.support.auth import TEST_PLATFORM_PASSWORD, login_platform, seed_platform_user
from tests.support.db import (
    clean_test_data,
    database_url,
    make_session_factory,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.integration, pytest.mark.business]

PDF_BYTES = b"%PDF-1.4\n% synthetic qualification\n%%EOF\n"
SELF_EXCEPTION_KEY = store.SELF_EXCEPTION_KEY
SUBMITTER = "e3-submitter"
REVIEWER = "e3-reviewer"


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
    """清空业务数据、种两个平台审核账号，并把应用指向测试栈（含 MailHog 与本地对象存储）。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
        for account in (SUBMITTER, REVIEWER):
            await seed_platform_user(
                admin_engine,
                account=account,
                password=TEST_PLATFORM_PASSWORD,
                permissions=("platform.tenant.review", "platform.tenant.view"),
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
    monkeypatch.delenv("DEV_SKIP_SCHOOL_REVIEW", raising=False)
    get_settings.cache_clear()

    yield

    await dispose_engine()
    get_settings.cache_clear()
    shutil.rmtree(object_dir, ignore_errors=True)


@pytest.fixture
async def client(env: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _scalar_rows(sql: str, **params: object) -> list[tuple[object, ...]]:
    """管理连接上执行一条只读 SQL（测试内取数助手，不经过应用会话）。"""

    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            rows = (await session.execute(text(sql), params)).all()
        return [tuple(row) for row in rows]
    finally:
        await engine.dispose()


async def _platform_user_id(account: str) -> UUID:
    rows = await _scalar_rows(
        "select id from platform_users where account = :account", account=account
    )
    assert len(rows) == 1, rows
    return UUID(str(rows[0][0]))


async def _seed_request(*, email: str, submitter_id: UUID | None) -> UUID:
    """插出一笔待审注册申请，并按"平台代提交"落上提交人列。

    为什么直插而不是走接口：本期只有校方自助提交通道，那一笔的提交人列是空的
    （没有平台账号，也就没有"自己审自己"可言）；平台代提交的入口要到 M2 运营后台。
    本测试取证的是**审核侧**对"代提交落地后的行形态"的判定，所以直接造出那种行。
    """

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
                    ":id, :school_name, '示范路 1 号', '张校长', :email, :object_key, "
                    ":sha256, :size, 'application/pdf', 'pending', now(), :submitter)"
                ),
                {
                    "id": registration_id,
                    "school_name": f"分权中学-{email.split('@')[0]}",
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


async def _enable_self_exception(editor_id: UUID) -> None:
    """打开自批例外（写库即生效：审核路径每请求从库读这个开关）。"""

    async with db_session(platform=True) as session:
        await store.put_setting(
            session,
            key=SELF_EXCEPTION_KEY,
            value=True,
            updated_by=editor_id,
            reason="双人都在出差，先由提交人自批这一笔（工单 AUD-9）",
        )


async def _audits(action: str) -> list[dict[str, object]]:
    rows = await _scalar_rows(
        "select resource_id, actor_id, result, metadata_json from audit_logs "
        "where action = :action order by sequence",
        action=action,
    )
    return [
        {
            "resource_id": row[0],
            "actor_id": row[1],
            "result": row[2],
            "metadata": row[3],
        }
        for row in rows
    ]


async def _registration(registration_id: UUID) -> TenantRegistrationRequest:
    async with db_session(platform=True) as session:
        request = await session.get(TenantRegistrationRequest, registration_id)
    assert request is not None
    return request


async def _review_records(registration_id: UUID) -> list[SchoolReviewRecord]:
    async with db_session(platform=True) as session:
        rows = (
            await session.execute(
                select(SchoolReviewRecord).where(SchoolReviewRecord.request_id == registration_id)
            )
        ).scalars().all()
    return list(rows)


@pytest.mark.asyncio
async def test_self_approval_is_forbidden_by_default(env: None) -> None:
    """默认（例外关闭）：提交人审核自己提交的申请 → 403，且申请仍可被别人审。"""

    submitter = await _platform_user_id(SUBMITTER)
    registration_id = await _seed_request(email="sod-forbidden@school.edu", submitter_id=submitter)

    with pytest.raises(OnboardingError) as error:
        await approve_request(registration_id, reviewer_id=submitter, reason="资质齐全")

    assert error.value.code == "SELF_APPROVAL_FORBIDDEN"
    assert error.value.http_status == 403
    assert (await _registration(registration_id)).status == "pending"
    assert await _review_records(registration_id) == []

    denied = await _audits("approval.self_approval.denied")
    assert len(denied) == 1, denied
    assert denied[0]["resource_id"] == str(registration_id)
    assert denied[0]["result"] == "denied"
    assert denied[0]["actor_id"] is not None and str(denied[0]["actor_id"]) == str(submitter)


@pytest.mark.asyncio
async def test_reject_path_is_also_gated(env: None) -> None:
    """拒绝同样算一次审批：提交人不能"自批自拒"，也不能借此抹掉待审状态。"""

    submitter = await _platform_user_id(SUBMITTER)
    registration_id = await _seed_request(email="sod-reject-gated@school.edu", submitter_id=submitter)

    with pytest.raises(OnboardingError) as error:
        await reject_request(registration_id, reviewer_id=submitter, reason="资质不清")

    assert error.value.code == "SELF_APPROVAL_FORBIDDEN"
    assert (await _registration(registration_id)).status == "pending"
    assert await _review_records(registration_id) == []


@pytest.mark.asyncio
async def test_other_reviewer_approves_and_mirrors_submitter(env: None) -> None:
    """正面控制：换一个人审核照常通过，且审核记录里的提交人与申请表一致（冗余列不是摆设）。"""

    submitter = await _platform_user_id(SUBMITTER)
    reviewer = await _platform_user_id(REVIEWER)
    registration_id = await _seed_request(email="sod-other@school.edu", submitter_id=submitter)

    outcome = await approve_request(
        registration_id, reviewer_id=reviewer, reason="资质齐全，同意开通"
    )

    records = await _review_records(registration_id)
    assert len(records) == 1
    record = records[0]
    assert record.tenant_id == outcome.tenant_id
    assert str(record.submitted_by_platform_user_id) == str(submitter)
    assert record.self_exception_used is False
    assert (await _registration(registration_id)).status == "approved"
    assert await _audits("approval.self_approval.denied") == []
    assert await _audits("approval.self_exception.used") == []


@pytest.mark.asyncio
async def test_exception_enabled_but_reason_missing_is_422(env: None) -> None:
    """例外开启但本次审批没写理由 → 422（例外不是"开关开了就随便自批"）。"""

    submitter = await _platform_user_id(SUBMITTER)
    registration_id = await _seed_request(email="sod-no-reason@school.edu", submitter_id=submitter)
    await _enable_self_exception(submitter)

    with pytest.raises(OnboardingError) as error:
        await approve_request(registration_id, reviewer_id=submitter, reason="   ")

    assert error.value.code == "SELF_APPROVAL_REASON_REQUIRED"
    assert error.value.http_status == 422
    assert (await _registration(registration_id)).status == "pending"
    assert await _audits("approval.self_exception.used") == []


@pytest.mark.asyncio
async def test_exception_enabled_hot_effect_and_audited(env: None) -> None:
    """改完开关不重启、不踢会话：下一次请求即用新值；用掉例外必须留带理由的审计。"""

    submitter = await _platform_user_id(SUBMITTER)
    registration_id = await _seed_request(email="sod-exception@school.edu", submitter_id=submitter)

    # 先证明默认关闭（同一进程、同一份配置缓存下就是 403）
    with pytest.raises(OnboardingError) as before:
        await approve_request(registration_id, reviewer_id=submitter, reason="自批说明")
    assert before.value.code == "SELF_APPROVAL_FORBIDDEN"

    await _enable_self_exception(submitter)
    outcome = await approve_request(
        registration_id, reviewer_id=submitter, reason="平台仅剩我一人在岗，工单 AUD-9"
    )

    assert outcome.school_code.startswith("SCH-")
    records = await _review_records(registration_id)
    assert len(records) == 1
    assert records[0].self_exception_used is True
    assert str(records[0].submitted_by_platform_user_id) == str(submitter)

    used = await _audits("approval.self_exception.used")
    assert len(used) == 1, used
    assert used[0]["resource_id"] == str(registration_id)
    assert used[0]["result"] == "success"
    metadata = used[0]["metadata"]
    assert isinstance(metadata, dict)
    assert "AUD-9" in metadata["reason"]


@pytest.mark.asyncio
async def test_database_check_blocks_direct_self_approval_write(env: None) -> None:
    """最后一道锁：绕过所有应用代码直接写审核记录，同人无例外标记必须被数据库拒绝。"""

    submitter = await _platform_user_id(SUBMITTER)
    registration_id = await _seed_request(email="sod-direct@school.edu", submitter_id=submitter)

    insert = """
        insert into school_review_records (
            id, request_id, tenant_id, reviewer_id, result, reason, reviewed_at,
            submitted_by_platform_user_id, self_exception_used
        ) values (
            gen_random_uuid(), :request_id, null, :reviewer, 'rejected', '手工直写', now(),
            :submitter, :exception_used
        )
    """

    engine = make_test_engine("admin")
    try:
        with pytest.raises(IntegrityError) as blocked:
            async with engine.begin() as connection:
                await connection.execute(
                    text(insert),
                    {
                        "request_id": registration_id,
                        "reviewer": submitter,
                        "submitter": submitter,
                        "exception_used": False,
                    },
                )
        assert "no_self_approval_without_exception" in str(blocked.value)

        # 正面控制：带例外标记即通过——证明约束挡的是"没走例外的同人审批"，不是一律禁止
        async with engine.begin() as connection:
            await connection.execute(
                text(insert),
                {
                    "request_id": registration_id,
                    "reviewer": submitter,
                    "submitter": submitter,
                    "exception_used": True,
                },
            )
    finally:
        await engine.dispose()

    rows = await _scalar_rows(
        "select self_exception_used from school_review_records where request_id = :id",
        id=registration_id,
    )
    assert [row[0] for row in rows] == [True]


async def _direct_review(
    registration_id: UUID, reviewer_id: UUID, reason: str | None
) -> None:
    """跳过接口层的第一道判定，直接走审核函数。

    模拟的是"应用层判过了、函数执行时开关已经变了"这个竞态窗口：本函数与接口层调用
    `review_registration_request()` 用的是同一条 SQL 与同一套错误映射，所以它既能证明
    数据库侧的第二道锁独立成立，也能证明锁返回的错误码能被翻译成干净的 4xx。
    """

    async with db_session(platform=True) as session, session.begin_nested():
        await _call_review(
            session,
            request_id_uuid=registration_id,
            result="approved",
            reason=reason,
            reviewer_id=reviewer_id,
            http_request_id=None,
            review_record_id=uuid4(),
            snapshot_key="onboarding/direct/snapshot.json",
            admin_id=None,
            password_hash=None,
            school_code="SCH-0000-0000",
        )


@pytest.mark.asyncio
async def test_database_function_gate_blocks_self_approval_without_app_layer(env: None) -> None:
    """第二道锁独立成立：绕过接口层直接调审核函数，DB 仍然拒绝，且错误映射为 403/422。"""

    submitter = await _platform_user_id(SUBMITTER)

    # 例外默认关闭：函数必须自己挡下，并把申请留在 pending、不产生审核记录
    forbidden_id = await _seed_request(
        email="sod-fn-forbidden@school.edu", submitter_id=submitter
    )
    with pytest.raises(OnboardingError) as forbidden:
        await _direct_review(forbidden_id, submitter, "绕过接口层的自批")
    assert forbidden.value.code == "SELF_APPROVAL_FORBIDDEN"
    assert forbidden.value.http_status == 403
    assert (await _registration(forbidden_id)).status == "pending"
    assert await _review_records(forbidden_id) == []

    # 例外已开但这一笔没带理由：函数给理由缺失，映射为 422（与接口层同一口径）
    await _enable_self_exception(submitter)
    reason_id = await _seed_request(email="sod-fn-reason@school.edu", submitter_id=submitter)
    with pytest.raises(OnboardingError) as missing_reason:
        await _direct_review(reason_id, submitter, "   ")
    assert missing_reason.value.code == "SELF_APPROVAL_REASON_REQUIRED"
    assert missing_reason.value.http_status == 422
    assert (await _registration(reason_id)).status == "pending"
    assert await _review_records(reason_id) == []


@pytest.mark.asyncio
async def test_review_endpoint_maps_self_approval_to_403(client: AsyncClient) -> None:
    """接口层：提交人自己审核拿到 403（不是 500），错误信封里不带内部 id。"""

    submitter = await _platform_user_id(SUBMITTER)
    registration_id = await _seed_request(email="sod-http@school.edu", submitter_id=submitter)

    headers = await login_platform(client, SUBMITTER, TEST_PLATFORM_PASSWORD)
    response = await client.post(
        f"/api/v1/platform/tenant-requests/{registration_id}/review",
        json={"result": "approved", "reason": "自己批"},
        headers=headers,
    )

    assert response.status_code == 403, response.text
    assert str(submitter) not in response.text
    assert (await _registration(registration_id)).status == "pending"
