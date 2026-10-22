"""业务场景 BS-M1-D2：MFA 登录挑战、Cookie 边界、/me 标记与平台强制绑定。"""

import os
from collections.abc import AsyncIterator, Iterator
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.auth import revocation
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.main import create_app
from tests.support.auth import TEST_PLATFORM_PASSWORD, seed_platform_user
from tests.support.db import (
    clean_test_data,
    database_url,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn
from tests.support.mfa import (
    FIXED_STEP,
    INVALID_CODE,
    audit_metadata,
    platform_refresh_rows,
    refresh_rows,
    seed_tenant,
    tenant_mfa_user,
)
from tests.support.mfa import (
    mfa_code as _code,
)

pytestmark = [pytest.mark.business, pytest.mark.integration]

SCHOOL_CODE = "SCH-BSMFA01"
TENANT_PASSWORD = "Tenant-Test-Pass-1"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    """迁移测试库并做一次性库护栏。"""

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
    """指向测试库/Redis 的应用客户端。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
    finally:
        await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    monkeypatch.setenv("MFA_REQUIRE_PLATFORM", "false")
    get_settings.cache_clear()
    await revocation.reset_client()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()


@pytest.fixture
def fixed_totp_step(monkeypatch: pytest.MonkeyPatch) -> int:
    """固定 TOTP 时钟，避免跨窗口抖动。"""

    monkeypatch.setattr("app.security.totp.current_step", lambda interval: FIXED_STEP)
    return FIXED_STEP


async def _seed_mfa_tenant() -> tuple[UUID, UUID, str]:
    """创建已启用 MFA 的租户用户。"""

    return await tenant_mfa_user("S-MFA-API", school_code=SCHOOL_CODE)


async def _seed_plain_tenant(school_code: str, login_id: str) -> tuple[UUID, UUID]:
    """创建未启用 MFA 的租户用户。"""

    return await seed_tenant(login_id, school_code=school_code)


@pytest.mark.asyncio
async def test_login_returns_challenge_without_cookie_then_verify_sets_session(
    client: AsyncClient, fixed_totp_step: int
) -> None:
    """密码正确 → 只返回挑战（无 Cookie/会话）；TOTP 正确 → 令牌 + HttpOnly Cookie。"""

    tenant_id, _, secret = await _seed_mfa_tenant()
    settings = get_settings()

    response = await client.post(
        "/api/v1/auth/login",
        json={
            "realm": "tenant",
            "school_code": SCHOOL_CODE,
            "login_id": "S-MFA-API",
            "password": TENANT_PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "mfa_required"
    assert body["methods"] == ["totp"]
    assert body["expires_in"] == settings.mfa_challenge_ttl_seconds
    assert "access_token" not in body
    assert settings.auth_cookie_name not in response.cookies
    assert "set-cookie" not in response.headers

    async with db_session(tenant_id=tenant_id) as session:
        count = await session.scalar(text("select count(*) from refresh_tokens"))
    assert count == 0

    wrong = await client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfa_token": body["mfa_token"], "code": INVALID_CODE},
    )
    assert wrong.status_code == 401
    assert "set-cookie" not in wrong.headers
    assert "access_token" not in wrong.text

    verified = await client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfa_token": body["mfa_token"], "code": _code(secret)},
    )
    assert verified.status_code == 200, verified.text
    tokens = verified.json()
    assert tokens["must_change_password"] is False
    cookie = verified.headers["set-cookie"]
    assert settings.auth_cookie_name in cookie
    assert "HttpOnly" in cookie

    me = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert me.status_code == 200, me.text
    assert me.json()["mfa_enabled"] is True
    assert me.json()["mfa_required"] is False

    # 浏览器端只用 Cookie 刷新（body 不传 refresh_token）
    refreshed = await client.post("/api/v1/auth/refresh", json={})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["access_token"]


@pytest.mark.asyncio
async def test_platform_forced_mfa_returns_enrollment_token_without_session(
    client: AsyncClient,
) -> None:
    """平台强制 MFA：未绑定账号只拿到绑定令牌（无 Cookie、无刷新行）。"""

    engine = make_test_engine("admin")
    try:
        await seed_platform_user(engine, account="ops-mfa-enroll")
    finally:
        await engine.dispose()

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MFA_REQUIRE_PLATFORM", "true")
        get_settings.cache_clear()

        response = await client.post(
            "/api/v1/auth/login",
            json={
                "realm": "platform",
                "account": "ops-mfa-enroll",
                "password": TEST_PLATFORM_PASSWORD,
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "mfa_enrollment_required"
    assert body["enroll_token"]
    assert "access_token" not in body
    assert "set-cookie" not in response.headers

    async with db_session(platform=True) as session:
        count = await session.scalar(text("select count(*) from platform_refresh_tokens"))
    assert count == 0


@pytest.mark.asyncio
async def test_login_still_authenticates_without_mfa(client: AsyncClient) -> None:
    """未开启 MFA 的账号保持原登录形态（带 status=authenticated，直接下发会话）。"""

    await _seed_plain_tenant("SCH-BSMFA02", "S-NO-MFA")

    response = await client.post(
        "/api/v1/auth/login",
        json={
            "realm": "tenant",
            "school_code": "SCH-BSMFA02",
            "login_id": "S-NO-MFA",
            "password": TENANT_PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "authenticated"
    assert body["access_token"]
    assert get_settings().auth_cookie_name in response.cookies


@pytest.mark.asyncio
async def test_self_enrollment_activation_and_disable_through_api(
    client: AsyncClient, fixed_totp_step: int
) -> None:
    """管理端安全设置页链路：绑定需密码、激活发恢复码并踢掉旧会话、关闭需密码+验证码。"""

    tenant_id, _ = await _seed_plain_tenant("SCH-BSMFA03", "S-ENR-API")
    settings = get_settings()

    login = await client.post(
        "/api/v1/auth/login",
        json={
            "realm": "tenant",
            "school_code": "SCH-BSMFA03",
            "login_id": "S-ENR-API",
            "password": TENANT_PASSWORD,
        },
    )
    assert login.status_code == 200, login.text
    old_access = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {old_access}"}

    missing_password = await client.post("/api/v1/auth/mfa/enroll", headers=headers, json={})
    assert missing_password.status_code == 400, missing_password.text
    wrong_password = await client.post(
        "/api/v1/auth/mfa/enroll", headers=headers, json={"current_password": "no-pass-12345"}
    )
    assert wrong_password.status_code == 401, wrong_password.text

    enrolled = await client.post(
        "/api/v1/auth/mfa/enroll",
        headers=headers,
        json={"current_password": TENANT_PASSWORD},
    )
    assert enrolled.status_code == 200, enrolled.text
    assert enrolled.headers["cache-control"] == "no-store"
    secret = enrolled.json()["secret"]
    assert enrolled.json()["otpauth_uri"].startswith("otpauth://totp/")
    assert enrolled.json()["expires_in"] == settings.mfa_enroll_token_ttl_seconds

    bad_code = await client.post(
        "/api/v1/auth/mfa/activate", headers=headers, json={"code": INVALID_CODE}
    )
    assert bad_code.status_code == 401, bad_code.text
    assert "set-cookie" not in bad_code.headers
    assert "recovery_codes" not in bad_code.text

    activated = await client.post(
        "/api/v1/auth/mfa/activate", headers=headers, json={"code": _code(secret)}
    )
    assert activated.status_code == 200, activated.text
    assert activated.headers["cache-control"] == "no-store"
    body = activated.json()
    assert len(body["recovery_codes"]) == settings.mfa_recovery_code_count
    assert "HttpOnly" in activated.headers["set-cookie"]
    new_headers = {"Authorization": f"Bearer {body['access_token']}"}

    # 激活轮换全部会话：旧访问令牌与旧 Cookie 立即失效
    stale_me = await client.get("/api/v1/auth/me", headers=headers)
    assert stale_me.status_code == 401, stale_me.text

    me = await client.get("/api/v1/auth/me", headers=new_headers)
    assert me.status_code == 200, me.text
    assert me.json()["mfa_enabled"] is True
    assert me.json()["mfa_required"] is False

    rows = await refresh_rows(tenant_id)
    assert [row["revoked_reason"] for row in rows] == ["mfa_change", None]
    async with db_session(tenant_id=tenant_id) as session:
        codes = await session.scalar(
            text("select count(*) from mfa_recovery_codes where tenant_id = :tid"),
            {"tid": tenant_id},
        )
    assert codes == settings.mfa_recovery_code_count

    # 绑定/激活的密钥与恢复码不得进入审计
    dumps = await audit_metadata("auth.mfa.activated")
    assert dumps[-1]["recovery_codes_issued"] == settings.mfa_recovery_code_count
    assert "secret" not in str(dumps)

    disabled = await client.post(
        "/api/v1/auth/mfa/disable",
        headers=new_headers,
        json={"current_password": TENANT_PASSWORD, "code": _code(secret, fixed_totp_step + 1)},
    )
    assert disabled.status_code == 200, disabled.text
    after = {"Authorization": f"Bearer {disabled.json()['access_token']}"}
    me_after = await client.get("/api/v1/auth/me", headers=after)
    assert me_after.json()["mfa_enabled"] is False
    async with db_session(tenant_id=tenant_id) as session:
        remaining = await session.scalar(
            text("select count(*) from mfa_recovery_codes where tenant_id = :tid"),
            {"tid": tenant_id},
        )
    assert remaining == 0

    again = await client.post(
        "/api/v1/auth/login",
        json={
            "realm": "tenant",
            "school_code": "SCH-BSMFA03",
            "login_id": "S-ENR-API",
            "password": TENANT_PASSWORD,
        },
    )
    assert again.json()["status"] == "authenticated"


@pytest.mark.asyncio
async def test_platform_forced_enrollment_chain_through_api(
    client: AsyncClient, fixed_totp_step: int
) -> None:
    """平台强制 MFA：绑定令牌免密码即可激活；策略开启期间不允许关闭。"""

    engine = make_test_engine("admin")
    try:
        await seed_platform_user(engine, account="ops-forced-api")
    finally:
        await engine.dispose()

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MFA_REQUIRE_PLATFORM", "true")
        get_settings.cache_clear()
        assert get_settings().mfa_require_platform is True

        login = await client.post(
            "/api/v1/auth/login",
            json={
                "realm": "platform",
                "account": "ops-forced-api",
                "password": TEST_PLATFORM_PASSWORD,
            },
        )
        assert login.status_code == 200, login.text
        assert login.json()["status"] == "mfa_enrollment_required"
        assert "set-cookie" not in login.headers
        enroll_headers = {"Authorization": f"Bearer {login.json()['enroll_token']}"}

        enrolled = await client.post("/api/v1/auth/mfa/enroll", headers=enroll_headers, json={})
        assert enrolled.status_code == 200, enrolled.text
        secret = enrolled.json()["secret"]

        activated = await client.post(
            "/api/v1/auth/mfa/activate", headers=enroll_headers, json={"code": _code(secret)}
        )
        assert activated.status_code == 200, activated.text
        access = {"Authorization": f"Bearer {activated.json()['access_token']}"}
        assert activated.json()["recovery_codes"]

        me = await client.get("/api/v1/auth/me", headers=access)
        assert me.json()["mfa_enabled"] is True
        assert me.json()["mfa_required"] is True

        blocked = await client.post(
            "/api/v1/auth/mfa/disable",
            headers=access,
            json={"current_password": TEST_PLATFORM_PASSWORD, "code": _code(secret)},
        )
        assert blocked.status_code == 403, blocked.text
        assert "不能关闭" in blocked.text

        rows = await platform_refresh_rows()
        assert rows and all(row["mfa_verified"] is True for row in rows)
