"""业务场景 BS-M1-B：学校注册 → 平台审核 → 自动开通 → 邮件送达（HTTP 全链路）。

断言业务输出：租户状态/学校编号/超管账号与角色/审核记录/审计事件/对象存储/邮件，
而不只是 HTTP 状态码。
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.auth.passwords import verify_password
from app.config import get_settings
from app.db.models.identity import User
from app.db.models.onboarding import SchoolReviewRecord
from app.db.models.rbac import Role, UserRole
from app.db.models.tenant import Tenant
from app.db.session import db_session, dispose_engine
from app.main import create_app
from app.storage.objects import build_object_store
from tests.support.db import (
    clean_test_data,
    database_url,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn
from tests.support.mailhog import extract_initial_password, message_body, wait_for_message

pytestmark = [pytest.mark.business, pytest.mark.integration]

PDF_BYTES = b"%PDF-1.4\n% bs-m1b qualification\n%%EOF\n"


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
async def client(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    """指向测试库/MailHog 的应用客户端（审核令牌来自测试栈）。"""

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
    monkeypatch.setenv("DEV_PLATFORM_REVIEW_TOKEN", os.environ["TEST_PLATFORM_REVIEW_TOKEN"])
    monkeypatch.delenv("DEV_SKIP_SCHOOL_REVIEW", raising=False)
    get_settings.cache_clear()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await dispose_engine()
    get_settings.cache_clear()
    import shutil

    shutil.rmtree(object_dir, ignore_errors=True)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['TEST_PLATFORM_REVIEW_TOKEN']}"}


async def test_bs_m1b_registration_to_activation(client: AsyncClient) -> None:
    """注册 → 审核通过 → 租户开通 → 邮件含可登录的初始密码 → 审计与审核记录可查。"""

    # 1) 学校提交注册（公开接口，multipart）
    response = await client.post(
        "/api/v1/platform/tenants/register",
        data={
            "school_name": "业务场景中学",
            "address": "场景路 8 号",
            "leadership": "李校长",
            "email": "Admin@Scenario.EDU",
        },
        files={"qualification": ("qualification.pdf", PDF_BYTES, "application/pdf")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["school_name_duplicate_hint"] is False
    request_id = body["request_id"]

    # 2) 未带令牌 / 带错令牌 → 拒绝
    denied = await client.post(
        f"/api/v1/platform/tenant-requests/{request_id}/review",
        json={"result": "approved"},
    )
    assert denied.status_code == 401
    wrong = await client.post(
        f"/api/v1/platform/tenant-requests/{request_id}/review",
        json={"result": "approved"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert wrong.status_code == 401

    # 3) 平台审核通过（临时开发令牌）
    review = await client.post(
        f"/api/v1/platform/tenant-requests/{request_id}/review",
        json={"result": "approved", "reason": "资质齐全"},
        headers=_auth_headers(),
    )
    assert review.status_code == 200, review.text
    review_body = review.json()
    assert review_body["status"] == "approved"
    assert review_body["email_sent"] is True
    school_code = review_body["school_code"]
    tenant_id = review_body["tenant_id"]
    assert school_code.startswith("SCH-")
    assert review_body["login_id"] == "admin@scenario.edu"
    # 响应体绝不能包含初始密码
    assert "password" not in review.text.lower()
    assert "初始密码" not in review.text

    # 4) 平台列表可查
    tenants = await client.get("/api/v1/platform/tenants", headers=_auth_headers())
    assert tenants.status_code == 200
    assert [item["school_code"] for item in tenants.json()] == [school_code]
    records = await client.get(
        f"/api/v1/platform/tenants/{tenant_id}/review-records", headers=_auth_headers()
    )
    assert records.status_code == 200
    assert [item["result"] for item in records.json()] == ["approved"]

    # 5) 数据库业务结果
    async with db_session(platform=True) as session:
        tenant = await session.get(Tenant, tenant_id)
        audit_actions = (await session.execute(select(SchoolReviewRecord))).scalars().all()
    assert tenant is not None
    assert tenant.status == "active"
    assert tenant.name == "业务场景中学"
    assert len(audit_actions) == 1

    async with db_session(tenant_id=tenant_id) as session:
        admin = (
            await session.execute(select(User).where(User.login_id == "admin@scenario.edu"))
        ).scalar_one()
        role_codes = {
            role.code
            for role in (await session.execute(select(Role))).scalars().all()
        }
        bindings = (await session.execute(select(UserRole))).scalars().all()

    assert admin.user_type == "school_admin"
    assert admin.must_change_password is True
    assert role_codes == {"school_super_admin", "school_admin", "teacher_b_end"}
    assert len(bindings) == 1

    # 6) 邮件：初始密码可登录（哈希校验）
    message = wait_for_message(
        os.environ["TEST_MAILHOG_API"], to="admin@scenario.edu", timeout=15
    )
    password = extract_initial_password(message_body(message))
    assert verify_password(password, admin.password_hash) is True

    # 7) 对象存储：凭证 + 审核快照
    store = build_object_store(get_settings())
    async with db_session(platform=True) as session:
        record = (
            await session.execute(select(SchoolReviewRecord))
        ).scalar_one()
    assert record.snapshot_object_key is not None
    assert await store.get(record.snapshot_object_key) == PDF_BYTES


async def test_bs_m1b_reject_flow(client: AsyncClient) -> None:
    """注册 → 拒绝（理由必填）→ 状态与理由可查，且不能再次审核。"""

    response = await client.post(
        "/api/v1/platform/tenants/register",
        data={
            "school_name": "被拒中学",
            "address": "拒绝路 1 号",
            "leadership": "王校长",
            "email": "reject@scenario.edu",
        },
        files={"qualification": ("q.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
    )
    assert response.status_code == 201
    request_id = response.json()["request_id"]

    missing_reason = await client.post(
        f"/api/v1/platform/tenant-requests/{request_id}/review",
        json={"result": "rejected"},
        headers=_auth_headers(),
    )
    assert missing_reason.status_code == 422

    rejected = await client.post(
        f"/api/v1/platform/tenant-requests/{request_id}/review",
        json={"result": "rejected", "reason": "资质不清晰"},
        headers=_auth_headers(),
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    listing = await client.get(
        "/api/v1/platform/tenant-requests",
        params={"status": "rejected"},
        headers=_auth_headers(),
    )
    assert listing.status_code == 200
    items = listing.json()
    assert [item["reject_reason"] for item in items] == ["资质不清晰"]


async def test_bs_m1b_oversized_request_is_rejected_before_parsing(
    client: AsyncClient,
) -> None:
    """M11：超过 11MB 的请求在 ASGI 层直接 413（不进入 multipart 解析）。"""

    response = await client.post(
        "/api/v1/platform/tenants/register",
        content=b"x" * (11 * 1024 * 1024 + 1),
        headers={"Content-Type": "multipart/form-data; boundary=----x"},
    )

    assert response.status_code == 413


async def test_bs_m1b_denied_review_is_audited_with_request_id(
    client: AsyncClient,
) -> None:
    """M7/M9：错误令牌被拒绝时写审计，且 request_id 与响应头一致。"""

    response = await client.post(
        "/api/v1/platform/tenants/register",
        data={
            "school_name": "越权审计中学",
            "address": "审计路 1 号",
            "leadership": "王校长",
            "email": "denied@school.edu",
        },
        files={"qualification": ("q.pdf", PDF_BYTES, "application/pdf")},
    )
    assert response.status_code == 201, response.text
    request_id = response.json()["request_id"]

    denied = await client.post(
        f"/api/v1/platform/tenant-requests/{request_id}/review",
        json={"result": "approved"},
        headers={"Authorization": "Bearer definitely-wrong"},
    )
    assert denied.status_code == 401
    header_request_id = denied.headers["x-request-id"]

    async with db_session(platform=True) as session:
        row = (
            await session.execute(
                text(
                    "select request_id, result from audit_logs "
                    "where action = 'platform.review.denied'"
                )
            )
        ).mappings().one()

    assert row["result"] == "failure"
    assert row["request_id"] == header_request_id


async def test_bs_m1b_chunked_oversized_request_is_rejected(
    client: AsyncClient,
) -> None:
    """M19：无 Content-Length 的 chunked 大请求同样在进入解析前 413。"""

    async def chunks() -> AsyncIterator[bytes]:
        block = b"x" * (1024 * 1024)
        for _ in range(12):
            yield block

    response = await client.post(
        "/api/v1/platform/tenants/register",
        content=chunks(),
        headers={"Content-Type": "multipart/form-data; boundary=----x"},
    )

    assert response.status_code == 413
    assert response.headers.get("x-request-id")


async def test_bs_m1b_body_limit_only_applies_to_register_route(
    client: AsyncClient,
) -> None:
    """M27：体积限制只作用于注册路由，其他路由的大请求不被 413 拦截。"""

    response = await client.post(
        "/api/v1/platform/not-a-real-route",
        content=b"x" * (12 * 1024 * 1024),
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 404
