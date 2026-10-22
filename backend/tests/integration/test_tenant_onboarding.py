"""M1-B 集成测试：注册、审核、开通、邮件、审计（真实 PG + MailHog + 本地对象存储）。"""

import asyncio
import os
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.auth.passwords import verify_password
from app.config import get_settings
from app.db.models.identity import User
from app.db.models.onboarding import SchoolReviewRecord, TenantRegistrationRequest
from app.db.models.rbac import Role, UserRole
from app.db.models.tenant import Tenant
from app.db.session import db_session, dispose_engine
from app.onboarding.service import (
    OnboardingError,
    RegistrationOutcome,
    approve_request,
    list_registration_requests,
    list_review_records,
    reject_request,
    submit_registration,
)
from app.storage.objects import build_object_store
from tests.support.db import (
    assert_role_is_not_superuser,
    clean_test_data,
    database_url,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn
from tests.support.mailhog import extract_initial_password, message_body, wait_for_message

pytestmark = [pytest.mark.integration, pytest.mark.business]

PDF_BYTES = b"%PDF-1.4\n% synthetic qualification\n%%EOF\n"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    """迁移测试库；对清理连接做一次性库护栏。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def onboarding_env(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Path]:
    """指向测试库/MailHog/本地对象存储，并清空业务数据。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
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

    yield object_dir

    await dispose_engine()
    get_settings.cache_clear()
    shutil.rmtree(object_dir, ignore_errors=True)


async def _submit(
    *,
    email: str = "admin@school.edu",
    school_name: str = "示范一中",
    request_id: str | None = None,
) -> RegistrationOutcome:
    """提交注册（默认合法 PDF 与学校信息）。"""

    return await submit_registration(
        school_name=school_name,
        address="示范路 1 号",
        leadership="张校长",
        email=email,
        filename="qualification.pdf",
        content_type="application/pdf",
        data=PDF_BYTES,
        request_id=request_id,
    )


@pytest.mark.asyncio
async def test_submit_registration_persists_request_file_and_audit(
    onboarding_env: Path,
) -> None:
    """提交注册：落库（规范化邮箱/sha256）、凭证入对象存储、写审计。"""

    outcome = await _submit(email="  Admin@School.EDU ", request_id="req-1")

    assert outcome.status == "pending"
    assert outcome.school_name_duplicate_hint is False

    async with db_session(platform=True) as session:
        request = await session.get(TenantRegistrationRequest, outcome.request_id)
        audit_actions: list[str] = list(
            (await session.execute(text("select action from audit_logs"))).scalars().all()
        )

    assert request is not None
    assert request.email == "admin@school.edu"
    assert request.status == "pending"
    assert request.school_name == "示范一中"
    assert request.qualification_object_key.startswith(f"onboarding/{outcome.request_id}/")
    assert len(request.qualification_sha256) == 64
    assert request.qualification_size == len(PDF_BYTES)

    store = build_object_store(get_settings())
    assert await store.get(request.qualification_object_key) == PDF_BYTES
    assert "tenant.register.submitted" in audit_actions


@pytest.mark.asyncio
async def test_duplicate_email_is_rejected(onboarding_env: Path) -> None:
    """同一邮箱（大小写/空白不同）重复提交 → 409。"""

    await _submit(email="admin@school.edu")

    with pytest.raises(OnboardingError) as excinfo:
        await _submit(email="  ADMIN@school.edu  ")

    assert excinfo.value.code == "DUPLICATE_EMAIL"
    assert excinfo.value.http_status == 409


@pytest.mark.asyncio
async def test_school_name_duplicate_hint(onboarding_env: Path) -> None:
    """校名已存在时只提示、不阻断。"""

    first = await _submit(email="a@school.edu", school_name="重复中学")
    second = await _submit(email="b@school.edu", school_name="重复中学")

    assert second.school_name_duplicate_hint is True
    assert first.request_id != second.request_id

    async with db_session(platform=True) as session:
        requests = (
            await session.execute(
                select(TenantRegistrationRequest).order_by(
                    TenantRegistrationRequest.submitted_at
                )
            )
        ).scalars().all()
    assert [request.status for request in requests] == ["pending", "pending"]


@pytest.mark.asyncio
async def test_approve_provisions_tenant_roles_admin_review_and_email(
    onboarding_env: Path,
) -> None:
    """审核通过：租户 active + 学校编号 + 3 个内置角色 + 超管账号 + 审核记录 + 邮件。"""

    submitted = await _submit(email="principal@school.edu")

    outcome = await approve_request(submitted.request_id, reviewer_id=uuid4(), reason="资质齐全")

    assert outcome.email_sent is True
    assert outcome.login_id == "principal@school.edu"
    assert outcome.school_code.startswith("SCH-")

    # 平台视角：租户与审核记录 + 审计
    async with db_session(platform=True) as session:
        tenant = await session.get(Tenant, outcome.tenant_id)
        records = (
            await session.execute(select(SchoolReviewRecord))
        ).scalars().all()
        audit_actions: list[str] = list(
            (await session.execute(text("select action from audit_logs"))).scalars().all()
        )
        request = await session.get(TenantRegistrationRequest, submitted.request_id)

    assert tenant is not None and tenant.status == "active"
    assert tenant.school_code == outcome.school_code
    assert tenant.address == "示范路 1 号"
    assert tenant.activated_at is not None
    assert len(records) == 1
    assert records[0].result == "approved"
    assert records[0].reason == "资质齐全"
    assert records[0].snapshot_object_key is not None
    assert request is not None and request.status == "approved"
    assert request.tenant_id == outcome.tenant_id
    assert "tenant.approve" in audit_actions

    # 租户视角（租户上下文）：5 个内置角色（M1-E1 起含 C 端 student/teacher）+ 超管账号与角色绑定
    async with db_session(tenant_id=outcome.tenant_id) as session:
        roles = (await session.execute(select(Role))).scalars().all()
        admin = (
            await session.execute(select(User).where(User.login_id == outcome.login_id))
        ).scalar_one()
        user_roles = (await session.execute(select(UserRole))).scalars().all()

    assert {role.code for role in roles} == {
        "school_super_admin",
        "school_admin",
        "teacher_b_end",
        "student",
        "teacher",
    }
    assert admin.user_type == "school_admin"
    assert admin.must_change_password is True
    assert admin.status == "active"
    assert {binding.role_id for binding in user_roles} == {
        role.id for role in roles if role.code == "school_super_admin"
    }

    # 邮件：正文含初始密码，且能校验通过数据库里的哈希（端到端）
    message = wait_for_message(
        os.environ["TEST_MAILHOG_API"], to="principal@school.edu", timeout=15
    )
    password = extract_initial_password(message_body(message))
    assert verify_password(password, admin.password_hash) is True

    # 审核快照对象存在
    store = build_object_store(get_settings())
    assert await store.get(records[0].snapshot_object_key) == PDF_BYTES


@pytest.mark.asyncio
async def test_reject_requires_reason_and_is_recorded(onboarding_env: Path) -> None:
    """拒绝必须填理由；拒绝后状态与审核记录正确，且不能再通过。"""

    submitted = await _submit(email="reject@school.edu")

    with pytest.raises(OnboardingError) as empty:
        await reject_request(submitted.request_id, reviewer_id=uuid4(), reason="  ")
    assert empty.value.code == "REASON_REQUIRED"
    assert empty.value.http_status == 422

    await reject_request(
        submitted.request_id, reviewer_id=uuid4(), reason="资质文件不清晰"
    )

    async with db_session(platform=True) as session:
        request = await session.get(TenantRegistrationRequest, submitted.request_id)
        records = (await session.execute(select(SchoolReviewRecord))).scalars().all()

    assert request is not None
    assert request.status == "rejected"
    assert request.reject_reason == "资质文件不清晰"
    assert [record.result for record in records] == ["rejected"]

    with pytest.raises(OnboardingError) as reviewed:
        await approve_request(submitted.request_id, reviewer_id=uuid4())
    assert reviewed.value.code == "ALREADY_REVIEWED"
    assert reviewed.value.http_status == 409


@pytest.mark.asyncio
async def test_concurrent_approve_only_once(onboarding_env: Path) -> None:
    """并发两次审核同一申请：只有一次成功，另一侧 409，且只创建一个租户。"""

    submitted = await _submit(email="race@school.edu")

    results = await asyncio.gather(
        approve_request(submitted.request_id, reviewer_id=uuid4()),
        approve_request(submitted.request_id, reviewer_id=uuid4()),
        return_exceptions=True,
    )

    successes = [item for item in results if not isinstance(item, Exception)]
    failures = [item for item in results if isinstance(item, OnboardingError)]

    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code == "ALREADY_REVIEWED"

    async with db_session(platform=True) as session:
        tenant_count = (
            await session.execute(text("select count(*) from tenants"))
        ).scalar()
        record_count = (
            await session.execute(text("select count(*) from school_review_records"))
        ).scalar()

    assert tenant_count == 1
    assert record_count == 1


@pytest.mark.asyncio
async def test_dev_skip_school_review_auto_approves(
    onboarding_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEV_SKIP_SCHOOL_REVIEW=true 时提交即开通（开发/测试专用）。"""

    monkeypatch.setenv("DEV_SKIP_SCHOOL_REVIEW", "true")
    get_settings.cache_clear()

    outcome = await _submit(email="skip@school.edu")

    assert outcome.status == "approved"
    assert outcome.email_sent is True

    async with db_session(platform=True) as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.email == "skip@school.edu"))
        ).scalar_one()
    assert tenant.status == "active"


@pytest.mark.asyncio
async def test_platform_listing_helpers(onboarding_env: Path) -> None:
    """平台侧列表：注册申请可按状态过滤；审核记录可按租户查询。"""

    submitted = await _submit(email="list@school.edu")
    outcome = await approve_request(submitted.request_id, reviewer_id=uuid4())
    await _submit(email="pending@school.edu")

    pending = await list_registration_requests(status="pending")
    approved = await list_registration_requests(status="approved")
    records = await list_review_records(outcome.tenant_id)

    assert [item.email for item in pending] == ["pending@school.edu"]
    assert [item.email for item in approved] == ["list@school.edu"]
    assert [record.result for record in records] == ["approved"]


@pytest.mark.asyncio
async def test_platform_roles_are_not_superusers(onboarding_env: Path) -> None:
    """平台/租户连接仍是非超级用户（RLS 生效的前提未被破坏）。"""

    platform_engine = make_test_engine("platform")
    tenant_engine = make_test_engine("tenant")
    try:
        await assert_role_is_not_superuser(platform_engine)
        await assert_role_is_not_superuser(tenant_engine)
    finally:
        await platform_engine.dispose()
        await tenant_engine.dispose()



# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 审查修复回归（B1 / M1 / M2 / M4 / M8 / M9 / M10 / M11 / M13）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_role_cannot_insert_identity_into_existing_tenant(
    onboarding_env: Path,
) -> None:
    """B1：平台连接只能 EXECUTE 开通函数，不能向存量租户写 users/roles（后门回归）。"""

    submitted = await _submit(email="victim@school.edu")
    approved = await approve_request(submitted.request_id, reviewer_id=uuid4())
    platform_engine = make_test_engine("platform")
    try:
        async with platform_engine.connect() as conn:
            await conn.execute(text("select set_config('app.platform_context', 'on', true)"))
            with pytest.raises(Exception) as user_error:
                await conn.execute(
                    text(
                        "insert into users (tenant_id, user_type, login_id, display_name) "
                        "values (:tenant_id, 'school_admin', 'backdoor@evil.test', 'backdoor')"
                    ),
                    {"tenant_id": approved.tenant_id},
                )
            assert "permission denied" in str(user_error.value).lower()

        async with platform_engine.connect() as conn:
            with pytest.raises(Exception) as role_error:
                await conn.execute(
                    text(
                        "insert into roles (tenant_id, code, name, is_system) "
                        "values (:tenant_id, 'backdoor', 'backdoor', true)"
                    ),
                    {"tenant_id": approved.tenant_id},
                )
            assert "permission denied" in str(role_error.value).lower()

        # M1-E1：`seed_builtin_roles(uuid)` 只由两个开通函数（SECURITY DEFINER）内部调用，
        # 平台连接不得直接 EXECUTE——否则拿到平台凭据就能对任意租户重种内置角色映射。
        async with platform_engine.connect() as conn:
            can_execute = await conn.scalar(
                text(
                    "select has_function_privilege("
                    "'campus_elf_platform', 'public.seed_builtin_roles(uuid)', 'execute')"
                )
            )
            assert can_execute is False, "平台连接被 grants 了 seed_builtin_roles 的执行权"
    finally:
        await platform_engine.dispose()


@pytest.mark.asyncio
async def test_super_admin_permissions_exclude_platform_realm(
    onboarding_env: Path,
) -> None:
    """M1：校方超管只获得 school/campus 模块权限，不含 platform.*。"""

    submitted = await _submit(email="realm@school.edu")
    approved = await approve_request(submitted.request_id, reviewer_id=uuid4())

    async with db_session(tenant_id=approved.tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "select p.code, p.module from permissions p "
                    "join role_permissions rp on rp.permission_id = p.id "
                    "join roles r on r.id = rp.role_id "
                    "where r.code = 'school_super_admin'"
                )
            )
        ).all()

    assert rows, "校方超管应至少获得校方模块权限"
    assert {row.module for row in rows} <= {"school", "campus"}
    assert not any(row.code.startswith("platform.") for row in rows)


@pytest.mark.asyncio
async def test_long_email_and_school_name_are_accepted_end_to_end(
    onboarding_env: Path,
) -> None:
    """M2：64 字符 local-part 邮箱 + 200 字校名可提交并通过审核（列宽不再 500）。"""

    email = "a" * 64 + "@school.edu"
    school_name = "长" * 200

    submitted = await _submit(email=email, school_name=school_name)
    outcome = await approve_request(submitted.request_id, reviewer_id=uuid4())

    async with db_session(tenant_id=outcome.tenant_id) as session:
        admin = (
            await session.execute(select(User).where(User.login_id == email))
        ).scalar_one()
    assert len(admin.display_name or "") <= 200
    assert (admin.display_name or "").startswith("长" * 10)


@pytest.mark.asyncio
async def test_email_delivery_failure_rolls_back_provisioning(
    onboarding_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4：SMTP 失败 → 502 且整笔回滚，不会出现"已激活但校方没有密码"。"""

    submitted = await _submit(email="smtp-fail@school.edu", request_id="req-smtp-fail")
    monkeypatch.setenv("SMTP_PORT", "1")
    get_settings.cache_clear()

    with pytest.raises(OnboardingError) as excinfo:
        await approve_request(
            submitted.request_id, reviewer_id=uuid4(), request_id="req-smtp-fail"
        )

    assert excinfo.value.code == "EMAIL_DELIVERY_FAILED"
    assert excinfo.value.http_status == 502

    async with db_session(platform=True) as session:
        request = await session.get(TenantRegistrationRequest, submitted.request_id)
        tenants = (await session.execute(text("select count(*) from tenants"))).scalar()
        records = (
            await session.execute(text("select count(*) from school_review_records"))
        ).scalar()
        approved_audits = (
            await session.execute(
                text("select count(*) from audit_logs where action = 'tenant.approve'")
            )
        ).scalar()

    assert request is not None and request.status == "pending"
    assert tenants == 0
    assert records == 0
    assert approved_audits == 0

    # 回滚后单独补写失败审计（租户不存在 → tenant_id 必须为空），且不含明文邮箱
    async with db_session(platform=True) as session:
        failure = (
            await session.execute(
                text(
                    "select result, tenant_id, request_id, metadata_json::text as metadata "
                    "from audit_logs where action = 'tenant.approve.email_failed'"
                )
            )
        ).mappings().one()
    assert failure["result"] == "failure"
    assert failure["tenant_id"] is None
    assert failure["request_id"] == "req-smtp-fail"
    assert "smtp-fail@school.edu" not in failure["metadata"]
    assert "s***@school.edu" in failure["metadata"]

    # 恢复 SMTP 后可重试成功
    monkeypatch.setenv("SMTP_PORT", os.environ["TEST_SMTP_PORT"])
    get_settings.cache_clear()
    outcome = await approve_request(submitted.request_id, reviewer_id=uuid4())
    assert outcome.email_sent is True


@pytest.mark.asyncio
async def test_audit_redacts_email_and_correlates_request_id(onboarding_env: Path) -> None:
    """M8/M9：审计不含明文邮箱，且 request_id 贯通提交/审核。"""

    submitted = await _submit(email="secret-address@school.edu", request_id="req-submit")
    outcome = await approve_request(
        submitted.request_id, reviewer_id=uuid4(), request_id="req-approve"
    )

    async with db_session(platform=True) as session:
        rows = (
            await session.execute(
                text(
                    "select action, request_id, metadata_json::text as metadata from audit_logs "
                    "where action in ('tenant.register.submitted', 'tenant.approve') "
                    "order by sequence"
                )
            )
        ).mappings().all()

    by_action = {row["action"]: row for row in rows}
    assert by_action["tenant.register.submitted"]["request_id"] == "req-submit"
    assert by_action["tenant.approve"]["request_id"] == "req-approve"
    assert "secret-address@school.edu" not in by_action["tenant.register.submitted"]["metadata"]
    assert "secret-address@school.edu" not in by_action["tenant.approve"]["metadata"]
    assert "s***@school.edu" in by_action["tenant.register.submitted"]["metadata"]
    assert outcome.tenant_id is not None


@pytest.mark.asyncio
async def test_review_records_are_append_only_and_status_locked(
    onboarding_env: Path,
) -> None:
    """M10：审核记录不可改/删；申请审结后不能再迁移状态。"""

    submitted = await _submit(email="immutable@school.edu")
    await approve_request(submitted.request_id, reviewer_id=uuid4())

    platform_engine = make_test_engine("platform")
    admin_engine = make_test_engine("admin")
    try:
        async with platform_engine.connect() as conn:
            with pytest.raises(Exception) as update_error:
                await conn.execute(text("update school_review_records set reason = 'x'"))
            assert "permission denied" in str(update_error.value).lower()

        async with platform_engine.connect() as conn:
            with pytest.raises(Exception) as delete_error:
                await conn.execute(text("delete from school_review_records"))
            assert "permission denied" in str(delete_error.value).lower()

        async with admin_engine.connect() as conn:
            with pytest.raises(Exception) as trigger_error:
                await conn.execute(text("update school_review_records set reason = 'x'"))
            assert "append-only" in str(trigger_error.value).lower()

        async with platform_engine.connect() as conn:
            with pytest.raises(Exception) as status_error:
                await conn.execute(
                    text(
                        "update tenant_registration_requests set status = 'rejected' "
                        "where id = :id"
                    ),
                    {"id": submitted.request_id},
                )
            # 平台已无 UPDATE 授权（M15）；即便有授权，状态机触发器也会拒绝
            assert "permission denied" in str(status_error.value).lower()
    finally:
        await platform_engine.dispose()
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_submission_leaves_no_orphan_object(onboarding_env: Path) -> None:
    """M11：重复邮箱被拒绝时不写入对象存储（先查重再落文件）。"""

    await _submit(email="dup-object@school.edu")
    files_before = sorted(path for path in onboarding_env.rglob("*") if path.is_file())

    with pytest.raises(OnboardingError) as excinfo:
        await _submit(email="dup-object@school.edu")
    assert excinfo.value.code == "DUPLICATE_EMAIL"

    files_after = sorted(path for path in onboarding_env.rglob("*") if path.is_file())
    assert files_after == files_before


@pytest.mark.asyncio
async def test_runtime_selfcheck_rejects_superuser_connection(
    onboarding_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M13/TD-073：启动自检拒绝超级用户连接；租户连接通过。"""

    from app.db.selfcheck import DatabaseSelfCheckError, verify_runtime_database

    monkeypatch.setenv("DATABASE_URL", database_url("admin"))
    get_settings.cache_clear()
    await dispose_engine()
    with pytest.raises(DatabaseSelfCheckError, match="超级用户"):
        await verify_runtime_database()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    get_settings.cache_clear()
    await dispose_engine()
    await verify_runtime_database()

    # B2：平台连接也必须是不可绕过 RLS 的角色
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("admin"))
    get_settings.cache_clear()
    await dispose_engine()
    with pytest.raises(DatabaseSelfCheckError, match="平台连接角色"):
        await verify_runtime_database()


@pytest.mark.asyncio
async def test_platform_direct_write_paths_are_revoked(onboarding_env: Path) -> None:
    """M15/M16：平台不能直接改申请状态/伪造审核记录/改租户身份/调用内部分通函数。"""

    submitted = await _submit(email="direct-write@school.edu")
    platform_engine = make_test_engine("platform")
    try:
        async with platform_engine.connect() as conn:
            await conn.execute(text("select set_config('app.platform_context', 'on', true)"))
            with pytest.raises(Exception) as update_error:
                await conn.execute(
                    text(
                        "update tenant_registration_requests set status = 'approved' "
                        "where id = :id"
                    ),
                    {"id": submitted.request_id},
                )
            assert "permission denied" in str(update_error.value).lower()

        async with platform_engine.connect() as conn:
            await conn.execute(text("select set_config('app.platform_context', 'on', true)"))
            with pytest.raises(Exception) as insert_error:
                await conn.execute(
                    text(
                        "insert into school_review_records "
                        "(request_id, reviewer_id, result) "
                        "values (:id, gen_random_uuid(), 'approved')"
                    ),
                    {"id": submitted.request_id},
                )
            assert "permission denied" in str(insert_error.value).lower()

        async with platform_engine.connect() as conn:
            await conn.execute(text("select set_config('app.platform_context', 'on', true)"))
            with pytest.raises(Exception) as tenant_error:
                await conn.execute(text("update tenants set name = 'tampered'"))
            assert "permission denied" in str(tenant_error.value).lower()

        # TD-094：公开注册只能走 submit_tenant_registration 函数，平台不能直接插申请单
        async with platform_engine.connect() as conn:
            await conn.execute(text("select set_config('app.platform_context', 'on', true)"))
            with pytest.raises(Exception) as insert_request_error:
                await conn.execute(
                    text(
                        "insert into tenant_registration_requests "
                        "(school_name, address, leadership, email, qualification_object_key, "
                        " qualification_sha256, qualification_size, qualification_mime, status) "
                        "values ('伪造中学', '伪造路', '伪造校长', 'fake@evil.test', "
                        " 'onboarding/fake/q.pdf', :digest, 10, 'application/pdf', 'pending')"
                    ),
                    {"digest": "0" * 64},
                )
            assert "permission denied" in str(insert_request_error.value).lower()

        async with platform_engine.connect() as conn:
            with pytest.raises(Exception) as function_error:
                await conn.execute(text("select provision_tenant('{}'::jsonb)"))
            assert "permission denied" in str(function_error.value).lower()

        # 租户角色同样不能调用审核函数
        tenant_engine = make_test_engine("tenant")
        try:
            async with tenant_engine.connect() as conn:
                with pytest.raises(Exception) as tenant_error:
                    await conn.execute(
                        text(
                            "select review_registration_request("
                            "gen_random_uuid(), 'rejected', 'x', gen_random_uuid(), "
                            "NULL, gen_random_uuid(), 'k', NULL, NULL, NULL)"
                        )
                    )
                assert "permission denied" in str(tenant_error.value).lower()
        finally:
            await tenant_engine.dispose()
    finally:
        await platform_engine.dispose()


@pytest.mark.asyncio
async def test_control_characters_in_school_name_are_rejected(
    onboarding_env: Path,
) -> None:
    """M18：校名含 CR/LF 时注册直接 422，不会形成"永远无法审核"的申请。"""

    bad_name = "注入中学" + chr(13) + chr(10) + "Bcc: victim@example.com"
    with pytest.raises(OnboardingError) as excinfo:
        await _submit(email="crlf@school.edu", school_name=bad_name)

    assert excinfo.value.code == "INVALID_FIELD"
    assert excinfo.value.http_status == 422


@pytest.mark.asyncio
async def test_review_function_ignores_shadow_temp_table(onboarding_env: Path) -> None:
    """B3：即使调用方建了同名 TEMP 表，审核函数也必须操作 public 的真实申请。"""

    submitted = await _submit(email="shadow@school.edu", school_name="真实中学")
    platform_engine = make_test_engine("platform")
    try:
        async with platform_engine.connect() as conn:
            await conn.execute(text("select set_config('app.platform_context', 'on', true)"))
            await conn.execute(
                text(
                    "create temp table tenant_registration_requests "
                    "(like public.tenant_registration_requests including all)"
                )
            )
            await conn.execute(
                text(
                    "insert into pg_temp.tenant_registration_requests "
                    "(id, school_name, address, leadership, email, "
                    " qualification_object_key, qualification_sha256, qualification_size, "
                    " qualification_mime, status) "
                    "values (:id, '影子中学', '影子路 1 号', '影子校长', 'shadow-admin@evil.test', "
                    " 'onboarding/shadow/qualification.pdf', :digest, 10, 'application/pdf', 'pending')"
                ),
                {"id": submitted.request_id, "digest": "0" * 64},
            )
            payload = await conn.scalar(
                text(
                    "select review_registration_request("
                    ":id, 'approved', '影子审核', :reviewer, 'req-shadow', :review_id, "
                    "'reviews/shadow/qualification.pdf', :admin_id, '$2b$12$fakehash', 'SCH-SHADOW1')"
                ),
                {
                    "id": submitted.request_id,
                    "reviewer": uuid4(),
                    "review_id": uuid4(),
                    "admin_id": uuid4(),
                },
            )
            assert payload["status"] == "approved"

            real_tenant = (
                await conn.execute(text("select name, school_code from public.tenants"))
            ).mappings().one()
            shadow_status = await conn.scalar(
                text("select status from pg_temp.tenant_registration_requests")
            )

        assert real_tenant["name"] == "真实中学"
        assert real_tenant["school_code"] == "SCH-SHADOW1"
        assert shadow_status == "pending"
    finally:
        await platform_engine.dispose()
