"""M1-D1 集成测试：邮件重置申请/确认（真实 PG + Redis + MailHog）。

覆盖：恒不泄露账号存在性、冷却与旧令牌作废、邮件令牌单次使用/过期、
密码策略与历史复用、锁定清零、全部会话撤销、平台 realm、邮件失败作废已提交令牌。
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import platform_admin
from app.auth import revocation, service, tokens
from app.auth.passwords import verify_password
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.main import create_app
from tests.support.auth import (
    TEST_PLATFORM_PASSWORD,
    as_token_pair,
    seed_platform_user,
    seed_tenant_user,
)
from tests.support.db import (
    clean_test_data,
    database_url,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn
from tests.support.mailhog import (
    extract_reset_token,
    extract_reset_url,
    message_body,
    wait_for_message,
)

pytestmark = [pytest.mark.integration, pytest.mark.business]

SCHOOL_CODE = "SCH-D1RESET1"
TENANT_PASSWORD = "Tenant-Test-Pass-1"
NEW_PASSWORD = "Reset-New-Pass-1"
CHANGED_PASSWORD = "Changed-Pass-77"


def _unique_email(prefix: str) -> str:
    """每次测试用独立收件地址，避免 MailHog 历史邮件干扰。"""

    return f"{prefix}-{uuid4().hex[:8]}@school.test"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    """确保测试库结构最新。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def reset_env(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """指向测试库/Redis/MailHog，清空业务数据（冷却默认关闭，专门用例再打开）。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
    finally:
        await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    monkeypatch.setenv("SMTP_HOST", os.environ["TEST_SMTP_HOST"])
    monkeypatch.setenv("SMTP_PORT", os.environ["TEST_SMTP_PORT"])
    monkeypatch.setenv("SMTP_FROM", "no-reply@campus-elf.test")
    monkeypatch.setenv("PASSWORD_RESET_URL_BASE", "http://127.0.0.1:5173/reset-password")
    monkeypatch.setenv("PASSWORD_RESET_REQUEST_COOLDOWN_SECONDS", "0")
    # 用例之间共享同一测试 IP；限流专项用例会单独调低并用独立 IP
    monkeypatch.setenv("PASSWORD_RESET_CONFIRM_IP_LIMIT", "100")
    get_settings.cache_clear()
    await revocation.reset_client()

    yield

    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()


@pytest.fixture
async def admin_engine(migrated_database: None) -> AsyncIterator[AsyncEngine]:
    """管理连接引擎（预置锁定/过期状态）。"""

    engine = make_test_engine("admin")
    yield engine
    await engine.dispose()


def _latest_reset_token(email: str) -> str:
    """等待 MailHog 收到最新重置邮件并取出令牌。"""

    message = wait_for_message(os.environ["TEST_MAILHOG_API"], to=email, timeout=15)
    return extract_reset_token(message_body(message))


async def _fresh_client_ip(scope: str) -> str:
    """取独立文档网段地址并清空其限流计数，避免跨运行/碰撞干扰。"""

    client_ip = f"198.18.{uuid4().int % 256}.{uuid4().int % 254 + 1}"
    ip_hash = tokens.hash_client_fingerprint(client_ip)
    assert ip_hash is not None
    await revocation.clear_rate_limit(scope, ip_hash)
    return client_ip


async def _audit_reasons(action: str) -> list[str]:
    """按时间顺序读取某动作的审计原因。"""

    async with db_session(platform=True) as session:
        values: list[object] = list(
            (
                await session.execute(
                    text(
                        "select metadata_json->>'reason' from audit_logs "
                        "where action = :action order by created_at, event_id"
                    ),
                    {"action": action},
                )
            )
            .scalars()
            .all()
        )
    return [str(value) for value in values]


async def _reset_rows(tenant_id: UUID) -> list[dict[str, Any]]:
    async with db_session(tenant_id=tenant_id) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select token_hash, expires_at, used_at, request_ip_hash "
                        "from password_reset_tokens order by created_at, id"
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _user_row(tenant_id: UUID, user_id: UUID) -> dict[str, Any]:
    async with db_session(tenant_id=tenant_id) as session:
        row = (
            (
                await session.execute(
                    text(
                        "select password_hash, must_change_password, token_version, "
                        "failed_login_attempts, failed_login_window_started_at, locked_until "
                        "from users where id = :uid"
                    ),
                    {"uid": user_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


@pytest.mark.asyncio
async def test_tenant_reset_request_and_confirm_full_flow(
    reset_env: None, admin_engine: AsyncEngine
) -> None:
    """申请 → 邮件 → 弱密码拒绝 → 确认 → 旧密码失效/历史/会话撤销/锁定清零。"""

    email = _unique_email("tenant-reset")
    tenant_id, user_id = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-4001",
        password=TENANT_PASSWORD,
        email=email,
    )
    pair = as_token_pair(
        await service.login_tenant(
            school_code=SCHOOL_CODE, login_id="S-4001", password=TENANT_PASSWORD
        )
    )
    # 锁定中的账号仍可申请重置，并应在重置成功后清零
    async with admin_engine.begin() as conn:
        await conn.execute(
            text(
                "update users set failed_login_attempts = 5, failed_login_window_started_at = now(), "
                "locked_until = now() + interval '15 minutes' where id = :uid"
            ),
            {"uid": user_id},
        )

    await service.request_password_reset(
        realm="tenant",
        school_code=SCHOOL_CODE,
        login_id="S-4001",
        ip="203.0.113.9",
    )

    token = _latest_reset_token(email)
    message = wait_for_message(os.environ["TEST_MAILHOG_API"], to=email, timeout=15)
    reset_url = extract_reset_url(message_body(message))
    assert "#token=" in reset_url
    assert "?token=" not in reset_url
    expected_secret = tokens.parse_password_reset_token(token)[2]
    rows = await _reset_rows(tenant_id)
    assert len(rows) == 1
    assert rows[0]["token_hash"] == tokens.hash_password_reset_token(expected_secret)
    assert rows[0]["used_at"] is None
    assert rows[0]["request_ip_hash"] == tokens.hash_client_fingerprint("203.0.113.9")
    assert (await _audit_reasons("auth.password.reset_requested")) == ["sent"]

    # 审计不得含令牌/邮箱明文
    async with db_session(platform=True) as session:
        audit_text = await session.scalar(
            text(
                "select string_agg(metadata_json::text, ' ') from audit_logs "
                "where action = 'auth.password.reset_requested'"
            )
        )
    assert token not in str(audit_text)
    assert expected_secret not in str(audit_text)
    assert email not in str(audit_text)

    with pytest.raises(service.AuthError) as weak:
        await service.confirm_password_reset(token=token, new_password="short1A")
    assert weak.value.http_status == 422
    assert (await _reset_rows(tenant_id))[0]["used_at"] is None

    await service.confirm_password_reset(
        token=token, new_password=NEW_PASSWORD, user_agent="pytest/1.0"
    )

    user = await _user_row(tenant_id, user_id)
    assert verify_password(NEW_PASSWORD, str(user["password_hash"]))
    assert user["must_change_password"] is False
    assert int(user["token_version"]) == 1
    assert user["failed_login_attempts"] == 0
    assert user["failed_login_window_started_at"] is None
    assert user["locked_until"] is None  # 重置成功解除锁定
    assert (await _reset_rows(tenant_id))[0]["used_at"] is not None

    async with db_session(tenant_id=tenant_id) as session:
        history: list[object] = list(
            (
                await session.execute(
                    text("select password_hash from password_history where user_id = :uid"),
                    {"uid": user_id},
                )
            )
            .scalars()
            .all()
        )
        refresh_reasons: list[object] = list(
            (
                await session.execute(
                    text(
                        "select distinct revoked_reason from refresh_tokens "
                        "where user_id = :uid and family_id = :fid"
                    ),
                    {"uid": user_id, "fid": pair.session_id},
                )
            )
            .scalars()
            .all()
        )
    assert len(history) == 1
    assert verify_password(TENANT_PASSWORD, str(history[0]))
    assert list(refresh_reasons) == ["password_reset"]

    # 旧访问令牌随会话标记删除立即失效
    claims = tokens.decode_access_token(get_settings(), pair.access_token)
    assert await revocation.is_active(str(claims.jti), pair.session_id, realm="tenant") is False
    async with db_session(platform=True) as session:
        reset_audit = await session.scalar(
            text("select count(*) from audit_logs where action = 'auth.password.reset'")
        )
    assert reset_audit == 1

    # 旧密码不能登录；新密码可以
    with pytest.raises(service.AuthError):
        await service.login_tenant(
            school_code=SCHOOL_CODE, login_id="S-4001", password=TENANT_PASSWORD
        )
    await service.login_tenant(school_code=SCHOOL_CODE, login_id="S-4001", password=NEW_PASSWORD)

    # 同一令牌重放 → 400，审计记录真实原因
    with pytest.raises(service.AuthError) as replay:
        await service.confirm_password_reset(token=token, new_password="Another-Pass-99")
    assert replay.value.http_status == 400
    assert replay.value.message == "重置链接无效或已过期"
    assert (await _audit_reasons("auth.password.reset_failed"))[-1] == "used"


@pytest.mark.asyncio
async def test_reset_rejects_password_history(reset_env: None, admin_engine: AsyncEngine) -> None:
    """重置同样受历史限制：旧密码/最近密码被拒，令牌保持可用。"""

    email = _unique_email("history-reset")
    tenant_id, user_id = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-4002",
        password=TENANT_PASSWORD,
        email=email,
    )
    await service.request_password_reset(realm="tenant", school_code=SCHOOL_CODE, login_id="S-4002")
    token = _latest_reset_token(email)
    await service.confirm_password_reset(token=token, new_password=NEW_PASSWORD)

    # 新申请（冷却已关闭）后用旧密码重置 → 422；令牌不被消费
    await service.request_password_reset(realm="tenant", school_code=SCHOOL_CODE, login_id="S-4002")
    token2 = _latest_reset_token(email)
    with pytest.raises(service.AuthError) as reuse:
        await service.confirm_password_reset(token=token2, new_password=TENANT_PASSWORD)
    assert reuse.value.http_status == 422
    assert "最近" in reuse.value.message
    unused = [row for row in await _reset_rows(tenant_id) if row["used_at"] is None]
    assert len(unused) == 1
    assert unused[0]["token_hash"] == tokens.hash_password_reset_token(
        tokens.parse_password_reset_token(token2)[2]
    )

    # 令牌仍可用于合规密码
    await service.confirm_password_reset(token=token2, new_password="Another-Pass-99")
    user = await _user_row(tenant_id, user_id)
    assert verify_password("Another-Pass-99", str(user["password_hash"]))


@pytest.mark.asyncio
async def test_reset_request_cooldown_and_old_token_invalidation(
    reset_env: None, admin_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """冷却期内不重复发信；冷却后新申请立即作废旧令牌。"""

    monkeypatch.setenv("PASSWORD_RESET_REQUEST_COOLDOWN_SECONDS", "60")
    get_settings.cache_clear()

    email = _unique_email("cooldown-reset")
    tenant_id, _ = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-4003",
        password=TENANT_PASSWORD,
        email=email,
    )
    await service.request_password_reset(realm="tenant", school_code=SCHOOL_CODE, login_id="S-4003")
    token1 = _latest_reset_token(email)

    await service.request_password_reset(realm="tenant", school_code=SCHOOL_CODE, login_id="S-4003")
    assert len(await _reset_rows(tenant_id)) == 1
    assert (await _audit_reasons("auth.password.reset_requested"))[-1] == "cooldown"

    # 把首次申请时间拨回冷却期外（不等待 60s）
    async with admin_engine.begin() as conn:
        await conn.execute(
            text("update password_reset_tokens set created_at = now() - interval '2 minutes'")
        )
    await service.request_password_reset(realm="tenant", school_code=SCHOOL_CODE, login_id="S-4003")
    token2 = _latest_reset_token(email)
    assert token2 != token1

    rows = await _reset_rows(tenant_id)
    assert len(rows) == 2
    assert rows[0]["used_at"] is not None
    assert rows[1]["used_at"] is None

    with pytest.raises(service.AuthError) as stale:
        await service.confirm_password_reset(token=token1, new_password=NEW_PASSWORD)
    assert stale.value.http_status == 400
    assert (await _audit_reasons("auth.password.reset_failed"))[-1] == "used"

    await service.confirm_password_reset(token=token2, new_password=NEW_PASSWORD)


@pytest.mark.asyncio
async def test_reset_token_expiry(reset_env: None, admin_engine: AsyncEngine) -> None:
    """过期令牌拒绝使用，审计记录 expired 且不改密码。"""

    email = _unique_email("expired-reset")
    tenant_id, user_id = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-4004",
        password=TENANT_PASSWORD,
        email=email,
    )
    await service.request_password_reset(realm="tenant", school_code=SCHOOL_CODE, login_id="S-4004")
    token = _latest_reset_token(email)
    async with admin_engine.begin() as conn:
        await conn.execute(
            text("update password_reset_tokens set expires_at = now() - interval '1 second'")
        )

    with pytest.raises(service.AuthError) as expired:
        await service.confirm_password_reset(token=token, new_password=NEW_PASSWORD)
    assert expired.value.http_status == 400
    assert (await _audit_reasons("auth.password.reset_failed"))[-1] == "expired"
    assert verify_password(
        TENANT_PASSWORD, str((await _user_row(tenant_id, user_id))["password_hash"])
    )
    assert (await _reset_rows(tenant_id))[0]["used_at"] is None


@pytest.mark.asyncio
async def test_reset_request_is_silent_for_unknown_or_emailless_accounts(
    reset_env: None, admin_engine: AsyncEngine
) -> None:
    """未知账号/无邮箱不抛错、不发信，只写真实原因审计。"""

    tenant_id, _ = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-4005",
        password=TENANT_PASSWORD,
        email=None,
    )
    await service.request_password_reset(realm="tenant", school_code=SCHOOL_CODE, login_id="S-4040")
    await service.request_password_reset(realm="tenant", school_code=SCHOOL_CODE, login_id="S-4005")
    await service.request_password_reset(
        realm="tenant", school_code="SCH-NOT-EXIST", login_id="S-4005"
    )

    assert await _reset_rows(tenant_id) == []
    reasons = await _audit_reasons("auth.password.reset_requested")
    assert reasons == ["unknown_account", "missing_email", "tenant_unavailable"]

    # 未知账号事件落平台并带 attempted_tenant_id，不能污染租户审计流
    async with db_session(platform=True) as session:
        unknown_metadata = await session.scalar(
            text(
                "select metadata_json from audit_logs "
                "where action = 'auth.password.reset_requested' "
                "and metadata_json->>'reason' = 'unknown_account'"
            )
        )
    assert unknown_metadata["attempted_tenant_id"] == str(tenant_id)
    async with db_session(tenant_id=tenant_id) as session:
        tenant_events = await session.scalar(
            text(
                "select count(*) from audit_logs "
                "where action = 'auth.password.reset_requested' "
                "and metadata_json->>'reason' = 'unknown_account'"
            )
        )
    assert tenant_events == 0


@pytest.mark.asyncio
async def test_reset_mail_failure_invalidates_committed_token(
    reset_env: None, admin_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """邮件投递失败：令牌已先提交，必须显式作废且只写失败审计。"""

    email = _unique_email("mailfail-reset")
    tenant_id, _ = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-4006",
        password=TENANT_PASSWORD,
        email=email,
    )

    class _FailingMailer:
        async def send(self, *, to: str, subject: str, body: str) -> None:
            raise RuntimeError("smtp down")

    monkeypatch.setattr(service, "build_mailer", lambda settings: _FailingMailer())

    await service.request_password_reset(realm="tenant", school_code=SCHOOL_CODE, login_id="S-4006")

    rows = await _reset_rows(tenant_id)
    assert len(rows) == 1
    assert rows[0]["used_at"] is not None

    reasons = await _audit_reasons("auth.password.reset_requested")
    assert reasons[-1] == "email_delivery_failed"
    assert "sent" not in reasons


@pytest.mark.asyncio
async def test_platform_reset_request_and_confirm(
    reset_env: None, admin_engine: AsyncEngine
) -> None:
    """平台账号重置：邮件令牌为平台前缀，改密/历史/会话撤销/登录均生效。"""

    email = _unique_email("platform-reset")
    account = "ops-reset-d1"
    await seed_platform_user(admin_engine, account=account, email=email)
    pair = as_token_pair(
        await service.login_platform(account=account, password=TEST_PLATFORM_PASSWORD)
    )

    await service.request_password_reset(realm="platform", account=account)
    token = _latest_reset_token(email)
    assert token.startswith("v1rp.")

    await service.confirm_password_reset(token=token, new_password=NEW_PASSWORD)

    async with db_session(platform=True) as session:
        user = (
            (
                await session.execute(
                    text(
                        "select id, password_hash, token_version from platform_users "
                        "where account = :account"
                    ),
                    {"account": account},
                )
            )
            .mappings()
            .one()
        )
        history: list[object] = list(
            (
                await session.execute(
                    text(
                        "select password_hash from platform_password_history "
                        "where platform_user_id = :uid"
                    ),
                    {"uid": user["id"]},
                )
            )
            .scalars()
            .all()
        )
        revoked: list[object] = list(
            (
                await session.execute(
                    text(
                        "select distinct revoked_reason from platform_refresh_tokens "
                        "where family_id = :fid"
                    ),
                    {"fid": pair.session_id},
                )
            )
            .scalars()
            .all()
        )
        token_used = await session.scalar(
            text("select used_at from platform_password_reset_tokens")
        )
    assert verify_password(NEW_PASSWORD, str(user["password_hash"]))
    assert int(user["token_version"]) == 1
    assert len(history) == 1 and verify_password(TEST_PLATFORM_PASSWORD, str(history[0]))
    assert list(revoked) == ["password_reset"]
    assert token_used is not None

    with pytest.raises(service.AuthError):
        await service.login_platform(account=account, password=TEST_PLATFORM_PASSWORD)
    await service.login_platform(account=account, password=NEW_PASSWORD)


@pytest.mark.asyncio
async def test_platform_account_provisioning_closes_email_reset_loop(
    reset_env: None, admin_engine: AsyncEngine
) -> None:
    """初始化脚本创建的账号可登录、可改邮箱，并能走完整的邮件重置链路。"""

    account = f"ops-{uuid4().hex[:24]}"
    email = _unique_email("ops-provision")
    # 迁移种子角色：本地测试库可能早于种子补丁建库，这里显式补齐（生产由迁移保证）
    async with admin_engine.begin() as conn:
        await conn.execute(
            text(
                "insert into platform_roles (code, name, is_system) "
                "values (:code, 'SaaS 超管', true) on conflict (code) do nothing"
            ),
            {"code": platform_admin.DEFAULT_PLATFORM_ROLE},
        )
    created = await platform_admin.provision_platform_account(account=account, email=email)
    assert created is not None
    assert created.role_code == platform_admin.DEFAULT_PLATFORM_ROLE

    pair = as_token_pair(
        await service.login_platform(account=account, password=created.initial_password)
    )
    assert pair.must_change_password is True

    with pytest.raises(platform_admin.PlatformAccountProvisionError):
        await platform_admin.provision_platform_account(account=account, email=email)

    # 改邮箱前已发出的重置链接必须随邮箱变更立即作废
    await service.request_password_reset(realm="platform", account=account)
    stale_token = _latest_reset_token(email)
    updated_email = _unique_email("ops-updated")
    assert (
        await platform_admin.provision_platform_account(
            account=account, email=updated_email, update_email_only=True
        )
        is None
    )
    with pytest.raises(service.AuthError) as stale:
        await service.confirm_password_reset(token=stale_token, new_password=NEW_PASSWORD)
    assert stale.value.http_status == 400

    await service.request_password_reset(realm="platform", account=account)
    token = _latest_reset_token(updated_email)
    assert token.startswith("v1rp.")
    await service.confirm_password_reset(token=token, new_password=NEW_PASSWORD)
    await service.login_platform(account=account, password=NEW_PASSWORD)

    async with db_session(platform=True) as session:
        user = (
            (
                await session.execute(
                    text(
                        "select id, email, must_change_password from platform_users "
                        "where account = :account"
                    ),
                    {"account": account},
                )
            )
            .mappings()
            .one()
        )
        actions: list[object] = list(
            (
                await session.execute(
                    text(
                        "select action from audit_logs "
                        "where action like 'platform.user.%' order by created_at, event_id"
                    )
                )
            )
            .scalars()
            .all()
        )
        role_count = await session.scalar(
            text("select count(*) from platform_user_roles where platform_user_id = :uid"),
            {"uid": user["id"]},
        )
    assert user["email"] == updated_email
    assert user["must_change_password"] is False
    assert role_count == 1
    assert "platform.user.provisioned" in actions
    assert "platform.user.email_updated" in actions


@pytest.mark.asyncio
async def test_password_reset_api_contract(reset_env: None, admin_engine: AsyncEngine) -> None:
    """HTTP 契约：申请恒 202（含未知账号）、确认 204、弱密码 422、重放 400。"""

    email = _unique_email("api-reset")
    await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-4007",
        password=TENANT_PASSWORD,
        email=email,
    )
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post(
            "/api/v1/auth/password-reset/request",
            json={"realm": "tenant", "school_code": SCHOOL_CODE, "login_id": "S-4007"},
        )
        assert accepted.status_code == 202
        assert accepted.json() == {"status": "accepted"}

        unknown = await client.post(
            "/api/v1/auth/password-reset/request",
            json={"realm": "tenant", "school_code": SCHOOL_CODE, "login_id": "S-4040"},
        )
        assert unknown.status_code == 202
        assert unknown.json() == {"status": "accepted"}

        missing = await client.post(
            "/api/v1/auth/password-reset/request",
            json={"realm": "tenant", "school_code": SCHOOL_CODE},
        )
        assert missing.status_code == 422

        token = _latest_reset_token(email)
        weak = await client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": "short1A"},
        )
        assert weak.status_code == 422

        confirmed = await client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": NEW_PASSWORD},
        )
        assert confirmed.status_code == 204
        assert confirmed.content == b""

        replay = await client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": "Another-Pass-99"},
        )
        assert replay.status_code == 400
        assert replay.json()["detail"] == "重置链接无效或已过期"

        malformed = await client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": "v1rt.not-a-uuid." + "x" * 40, "new_password": NEW_PASSWORD},
        )
        assert malformed.status_code == 400

        too_short = await client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": "bad", "new_password": NEW_PASSWORD},
        )
        assert too_short.status_code == 400


@pytest.mark.asyncio
async def test_confirm_rate_limit_stops_audit_amplification(
    reset_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未认证确认请求按 IP 限流：超限返回 429 且不再写失败审计。"""

    monkeypatch.setenv("PASSWORD_RESET_CONFIRM_IP_LIMIT", "2")
    monkeypatch.setenv("PASSWORD_RESET_IP_WINDOW_SECONDS", "600")
    get_settings.cache_clear()
    client_ip = await _fresh_client_ip("password_reset_confirm")
    # 格式合法但租户/令牌不存在：每次请求都会写一条平台失败审计
    token = tokens.generate_password_reset_token("tenant", uuid4())
    transport = ASGITransport(app=create_app(), client=(client_ip, 5000))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first_two = [
            await client.post(
                "/api/v1/auth/password-reset/confirm",
                json={"token": token, "new_password": NEW_PASSWORD},
            )
            for _ in range(2)
        ]
        assert [response.status_code for response in first_two] == [400, 400]
        assert len(await _audit_reasons("auth.password.reset_failed")) == 2

        limited = await client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": NEW_PASSWORD},
        )

    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "600"
    # 第 3 次在解析/查询前被限流拦截，审计条数不增长
    assert len(await _audit_reasons("auth.password.reset_failed")) == 2


@pytest.mark.asyncio
async def test_school_admin_login_email_is_backfilled_and_can_reset(
    reset_env: None, admin_engine: AsyncEngine
) -> None:
    """校管登录名即邮箱：触发器回填 + 邮件重置全链路可用（M2 平台侧已覆盖）。"""

    login_id = "admin-d1@school.test"
    tenant_id, user_id = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id=login_id,
        password=TENANT_PASSWORD,
        email=None,
    )
    async with db_session(tenant_id=tenant_id) as session:
        email = await session.scalar(
            text("select email from users where id = :uid"), {"uid": user_id}
        )
    assert email == login_id

    await service.request_password_reset(realm="tenant", school_code=SCHOOL_CODE, login_id=login_id)
    token = _latest_reset_token(login_id)
    await service.confirm_password_reset(token=token, new_password=NEW_PASSWORD)
    await service.login_tenant(school_code=SCHOOL_CODE, login_id=login_id, password=NEW_PASSWORD)


@pytest.mark.asyncio
async def test_reset_failure_audit_does_not_leak_token(
    reset_env: None, admin_engine: AsyncEngine
) -> None:
    """确认失败审计只记录原因与哈希，不写令牌/密码。"""

    email = _unique_email("audit-reset")
    await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-4008",
        password=TENANT_PASSWORD,
        email=email,
    )
    await service.request_password_reset(realm="tenant", school_code=SCHOOL_CODE, login_id="S-4008")
    token = _latest_reset_token(email)
    await service.confirm_password_reset(token=token, new_password=NEW_PASSWORD)
    with pytest.raises(service.AuthError):
        await service.confirm_password_reset(token=token, new_password="Another-Pass-99")

    async with db_session(platform=True) as session:
        audit_text = str(
            await session.scalar(
                text(
                    "select string_agg(metadata_json::text, ' ') from audit_logs "
                    "where action like 'auth.password.reset%'"
                )
            )
        )
    assert token not in audit_text
    assert tokens.parse_password_reset_token(token)[2] not in audit_text
    assert NEW_PASSWORD not in audit_text


def test_reset_token_format_roundtrip() -> None:
    """令牌格式/哈希：租户带 tenant_id、平台不带；数据库哈希稳定。"""

    tenant_id = UUID("11111111-1111-1111-1111-111111111111")
    tenant_token = tokens.generate_password_reset_token("tenant", tenant_id)
    realm, parsed_tenant, secret = tokens.parse_password_reset_token(tenant_token)
    assert realm == "tenant"
    assert parsed_tenant == tenant_id
    assert len(secret) >= 32
    assert tokens.hash_password_reset_token(secret) == tokens.hash_password_reset_token(secret)

    platform_token = tokens.generate_password_reset_token("platform", None)
    realm, parsed_tenant, _ = tokens.parse_password_reset_token(platform_token)
    assert realm == "platform"
    assert parsed_tenant is None

    with pytest.raises(tokens.TokenError):
        tokens.parse_password_reset_token("v1rt.not-a-uuid." + "x" * 40)
    with pytest.raises(ValueError):
        tokens.generate_password_reset_token("tenant", None)
    with pytest.raises(ValueError):
        tokens.generate_password_reset_token("platform", tenant_id)


@pytest.mark.asyncio
async def test_reset_uses_utc_expiry(reset_env: None, admin_engine: AsyncEngine) -> None:
    """令牌过期时间按 UTC 计算（距现在约 TTL，带时区）。"""

    email = _unique_email("ttl-reset")
    tenant_id, _ = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-4009",
        password=TENANT_PASSWORD,
        email=email,
    )
    await service.request_password_reset(realm="tenant", school_code=SCHOOL_CODE, login_id="S-4009")
    rows = await _reset_rows(tenant_id)
    expires_at = rows[0]["expires_at"]
    assert expires_at.tzinfo is not None
    remaining = expires_at - datetime.now(UTC)
    assert timedelta(minutes=25) <= remaining <= timedelta(minutes=31)


@pytest.mark.asyncio
async def test_student_self_reset_is_deferred(reset_env: None, admin_engine: AsyncEngine) -> None:
    """学生自助重置延后到 M2：不发邮件、不建令牌，只记审计原因。"""

    email = _unique_email("student-reset")
    tenant_id, _ = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-4013",
        password=TENANT_PASSWORD,
        email=email,
        user_type="student",
    )

    await service.request_password_reset(realm="tenant", school_code=SCHOOL_CODE, login_id="S-4013")

    assert await _reset_rows(tenant_id) == []
    assert (await _audit_reasons("auth.password.reset_requested"))[-1] == ("student_reset_deferred")


@pytest.mark.asyncio
async def test_change_password_invalidates_unused_reset_token(
    reset_env: None, admin_engine: AsyncEngine
) -> None:
    """改密成功后未使用的重置链接立即失效，不能把密码改回去。"""

    email = _unique_email("change-reset")
    tenant_id, user_id = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-4014",
        password=TENANT_PASSWORD,
        email=email,
    )
    await service.login_tenant(school_code=SCHOOL_CODE, login_id="S-4014", password=TENANT_PASSWORD)
    await service.request_password_reset(realm="tenant", school_code=SCHOOL_CODE, login_id="S-4014")
    token = _latest_reset_token(email)

    await service.change_password(
        principal_user_id=user_id,
        realm="tenant",
        tenant_id=tenant_id,
        current_password=TENANT_PASSWORD,
        new_password=CHANGED_PASSWORD,
    )

    rows = await _reset_rows(tenant_id)
    assert len(rows) == 1 and rows[0]["used_at"] is not None
    with pytest.raises(service.AuthError) as stale:
        await service.confirm_password_reset(token=token, new_password="Another-Pass-99")
    assert stale.value.http_status == 400
    assert (await _audit_reasons("auth.password.reset_failed"))[-1] == "used"

    user = await _user_row(tenant_id, user_id)
    assert verify_password(CHANGED_PASSWORD, str(user["password_hash"]))


@pytest.mark.asyncio
async def test_reset_token_cannot_cross_tenant_and_audit_stays_platform(
    reset_env: None, admin_engine: AsyncEngine
) -> None:
    """把令牌 tenant_id 换成另一租户：拒绝且不得往该租户审计流写失败记录。"""

    email = _unique_email("cross-tenant")
    tenant_a, user_a = await seed_tenant_user(
        admin_engine,
        school_code="SCH-D1CROSSA",
        login_id="S-4015",
        password=TENANT_PASSWORD,
        email=email,
    )
    tenant_b, _ = await seed_tenant_user(
        admin_engine,
        school_code="SCH-D1CROSSB",
        login_id="S-4016",
        password=TENANT_PASSWORD,
    )
    await service.request_password_reset(
        realm="tenant", school_code="SCH-D1CROSSA", login_id="S-4015"
    )
    token = _latest_reset_token(email)
    _kind, _tenant, secret = tokens.parse_password_reset_token(token)
    forged = f"v1rt.{tenant_b}.{secret}"

    with pytest.raises(service.AuthError) as rejected:
        await service.confirm_password_reset(token=forged, new_password=NEW_PASSWORD)
    assert rejected.value.http_status == 400

    user = await _user_row(tenant_a, user_a)
    assert verify_password(TENANT_PASSWORD, str(user["password_hash"]))
    async with db_session(platform=True) as session:
        tenant_b_audit = await session.scalar(
            text(
                "select count(*) from audit_logs "
                "where tenant_id = :tid and action = 'auth.password.reset_failed'"
            ),
            {"tid": tenant_b},
        )
        attempted = await session.scalar(
            text(
                "select metadata_json->>'attempted_tenant_id' from audit_logs "
                "where action = 'auth.password.reset_failed' "
                "order by created_at desc, event_id desc limit 1"
            )
        )
    assert tenant_b_audit == 0
    assert attempted == str(tenant_b)


@pytest.mark.asyncio
async def test_concurrent_reset_confirmation_only_one_wins(
    reset_env: None, admin_engine: AsyncEngine
) -> None:
    """同一令牌并发确认：只有一个成功改密，另一个按已使用拒绝。"""

    email = _unique_email("concurrent-reset")
    tenant_id, user_id = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-4017",
        password=TENANT_PASSWORD,
        email=email,
    )
    await service.request_password_reset(realm="tenant", school_code=SCHOOL_CODE, login_id="S-4017")
    token = _latest_reset_token(email)

    results = await asyncio.gather(
        service.confirm_password_reset(token=token, new_password="Concurrent-Pass-1"),
        service.confirm_password_reset(token=token, new_password="Concurrent-Pass-2"),
        return_exceptions=True,
    )
    failures = [item for item in results if isinstance(item, service.AuthError)]
    assert len(failures) == 1 and failures[0].http_status == 400

    user = await _user_row(tenant_id, user_id)
    password_hash = str(user["password_hash"])
    assert verify_password("Concurrent-Pass-1", password_hash) or verify_password(
        "Concurrent-Pass-2", password_hash
    )
    assert (await _reset_rows(tenant_id))[0]["used_at"] is not None


@pytest.mark.asyncio
async def test_reset_request_ip_rate_limit(
    reset_env: None, admin_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一 IP 超过窗口上限后不再发信/建令牌，也不写限流审计（接口仍 202）。"""

    monkeypatch.setenv("PASSWORD_RESET_IP_LIMIT", "2")
    monkeypatch.setenv("PASSWORD_RESET_IP_WINDOW_SECONDS", "900")
    get_settings.cache_clear()

    email = _unique_email("ratelimit-reset")
    tenant_id, _ = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-4018",
        password=TENANT_PASSWORD,
        email=email,
    )
    client_ip = await _fresh_client_ip("password_reset")

    for _ in range(3):
        await service.request_password_reset(
            realm="tenant",
            school_code=SCHOOL_CODE,
            login_id="S-4018",
            ip=client_ip,
        )

    assert len(await _reset_rows(tenant_id)) == 2
    reasons = await _audit_reasons("auth.password.reset_requested")
    # 只有前两次成功发送写审计；第 3 次静默丢弃，避免未认证请求放大审计写入
    assert reasons == ["sent", "sent"]


@pytest.mark.asyncio
async def test_validation_error_does_not_echo_password(
    reset_env: None, admin_engine: AsyncEngine
) -> None:
    """422 响应不得回显请求字段原值（密码/令牌），只回位置与原因。"""

    marker = "DoNotEcho-Secret-9"
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/auth/password-reset/confirm",
            json={
                "token": f"v1rt.{uuid4()}.{'x' * 40}",
                "new_password": marker + "Aa1!" + "y" * 140,
            },
        )

    assert response.status_code == 422
    assert marker not in response.text
    details = response.json()["detail"]
    assert details
    assert all("input" not in item for item in details)
