"""M1-D2 集成测试：MFA 绑定/激活/关闭与平台运维重置（真实 PG + Redis）。

覆盖点：绑定必须重新验证密码、待激活 secret 只存 Redis 且单次使用、激活/关闭都
轮换全部会话（token_version+1 + 撤销刷新家族 + 删除 Redis 会话标记）、恢复码由激活
生成并可登录/关闭、平台强制策略下的绑定链路与运维 `--reset-mfa` 兜底。
"""

import os
from collections.abc import AsyncIterator, Iterator
from uuid import UUID

import pytest
from sqlalchemy import text

from app import platform_admin
from app.auth import revocation, service
from app.auth.mfa import decode_mfa_token
from app.auth.tokens import decode_access_token
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.security.totp import generate_totp_secret
from tests.support.auth import TEST_PLATFORM_PASSWORD, seed_platform_user
from tests.support.db import (
    clean_test_data,
    database_url,
    make_session_factory,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn
from tests.support.mfa import (
    FIXED_STEP,
    INVALID_CODE,
    SCHOOL_CODE,
    TENANT_PASSWORD,
)
from tests.support.mfa import (
    audit_metadata as _audit_metadata,
)
from tests.support.mfa import (
    enable_platform_mfa as _enable_platform_mfa,
)
from tests.support.mfa import (
    mfa_code as _code,
)
from tests.support.mfa import (
    platform_mfa_state as _platform_mfa_state,
)
from tests.support.mfa import (
    platform_refresh_rows as _platform_refresh_rows,
)
from tests.support.mfa import (
    platform_user_id as _platform_user_id,
)
from tests.support.mfa import (
    refresh_rows as _refresh_rows,
)
from tests.support.mfa import (
    seed_tenant as _seed_tenant,
)
from tests.support.mfa import (
    tenant_login as _tenant_login,
)
from tests.support.mfa import (
    tenant_mfa_state as _tenant_mfa_state,
)

pytestmark = [pytest.mark.integration]


def _token_version(pair: service.TokenPair) -> int:
    """读取已签发访问令牌的凭证版本。"""

    return int(decode_access_token(get_settings(), pair.access_token).token_version)


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
async def mfa_env(migrated_database: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """指向测试库/Redis，并清空业务数据。"""

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

    yield

    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()


@pytest.fixture
def fixed_totp_step(monkeypatch: pytest.MonkeyPatch) -> int:
    """固定 TOTP 时钟，避免测试跨 30 秒窗口抖动。"""

    monkeypatch.setattr("app.security.totp.current_step", lambda interval: FIXED_STEP)
    return FIXED_STEP


def _tenant_actor(
    tenant_id: UUID, user_id: UUID, *, token_version: int | None = None
) -> service.MfaActor:
    """访问令牌路径的绑定主体（需重新验证密码）。"""

    return service.MfaActor(
        user_id=user_id, realm="tenant", tenant_id=tenant_id, token_version=token_version
    )


async def _platform_actor_from_enroll_token(
    enroll_token: str,
) -> service.MfaActor:
    """强制绑定令牌路径的主体（登录时已验证密码）。"""

    claims = decode_mfa_token(get_settings(), enroll_token, expected_purpose="mfa_enroll")
    return service.MfaActor(
        user_id=claims.subject,
        realm=claims.realm,
        tenant_id=claims.tenant_id,
        token_version=claims.token_version,
        from_enroll_token=True,
    )


async def _normal_tenant_login(login_id: str) -> service.TokenPair:
    """未启用 MFA 时的密码登录（返回会话）。"""

    outcome = await service.login_tenant(
        school_code=SCHOOL_CODE, login_id=login_id, password=TENANT_PASSWORD
    )
    assert isinstance(outcome, service.TokenPair)
    return outcome


async def _recovery_code_rows(tenant_id: UUID, user_id: UUID) -> list[dict[str, object]]:
    """读取恢复码行（只含使用状态，不含摘要）。"""

    async with db_session(tenant_id=tenant_id) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select used_at from mfa_recovery_codes "
                        "where tenant_id = :tid and user_id = :uid"
                    ),
                    {"tid": tenant_id, "uid": user_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _bump_tenant_token_version(tenant_id: UUID, user_id: UUID) -> None:
    """直接自增 token_version（模拟改密/运维踢出后的旧令牌）。"""

    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "update users set token_version = token_version + 1 "
                    "where tenant_id = :tid and id = :uid"
                ),
                {"tid": tenant_id, "uid": user_id},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_enroll_requires_password_and_stores_pending_secret(
    mfa_env: None, fixed_totp_step: int
) -> None:
    """访问令牌路径：缺密码 400、密码错误计入锁定且不落 secret；正确密码才生成待激活项。"""

    tenant_id, user_id = await _seed_tenant("S-ENR-1")
    actor = _tenant_actor(tenant_id, user_id)
    pair = await _normal_tenant_login("S-ENR-1")

    with pytest.raises(service.AuthError) as missing:
        await service.enroll_mfa(actor=actor, current_password=None)
    assert missing.value.code == "PASSWORD_REQUIRED"
    assert missing.value.http_status == 400
    assert await revocation.get_mfa_pending_secret(realm="tenant", user_id=user_id) is None

    with pytest.raises(service.AuthError) as wrong:
        await service.enroll_mfa(actor=actor, current_password="wrong-password-1")
    assert wrong.value.code == "INVALID_CREDENTIALS"
    state = await _tenant_mfa_state(tenant_id, user_id)
    assert state["failed_login_attempts"] == 1
    assert state["mfa_enabled"] is False
    assert await revocation.get_mfa_pending_secret(realm="tenant", user_id=user_id) is None
    failed = await _audit_metadata("auth.mfa.enrollment_failed")
    assert failed[-1]["reason"] == "password_mismatch"

    enrollment = await service.enroll_mfa(actor=actor, current_password=TENANT_PASSWORD)
    assert enrollment.expires_in == get_settings().mfa_enroll_token_ttl_seconds
    assert "S-ENR-1" in enrollment.otpauth_uri
    assert await revocation.get_mfa_pending_secret(realm="tenant", user_id=user_id) is not None
    assert len(await _audit_metadata("auth.mfa.enrollment_started")) == 1

    # 绑定阶段不影响已签发会话
    refreshed = await service.refresh_tokens(refresh_token=pair.refresh_token)
    assert refreshed.user_id == user_id


@pytest.mark.asyncio
async def test_activation_enables_mfa_and_rotates_all_sessions(
    mfa_env: None, fixed_totp_step: int
) -> None:
    """激活：10 枚恢复码、token_version+1、旧家族撤销并删除会话标记、旧刷新令牌失效。"""

    tenant_id, user_id = await _seed_tenant("S-ENR-2")
    actor = _tenant_actor(tenant_id, user_id)
    old_pair = await _normal_tenant_login("S-ENR-2")
    enrollment = await service.enroll_mfa(actor=actor, current_password=TENANT_PASSWORD)

    with pytest.raises(service.AuthError) as bad_code:
        await service.activate_mfa(actor=actor, code=INVALID_CODE)
    assert bad_code.value.code == "INVALID_CREDENTIALS"
    state = await _tenant_mfa_state(tenant_id, user_id)
    assert state["mfa_enabled"] is False
    assert state["failed_login_attempts"] == 1
    assert (await _audit_metadata("auth.mfa.activation_failed"))[-1]["reason"] == "invalid_mfa_code"
    # 验证码错误不得消费待激活 secret（仍可重试）
    assert await revocation.get_mfa_pending_secret(realm="tenant", user_id=user_id) is not None

    old_version = _token_version(old_pair)
    activation = await service.activate_mfa(actor=actor, code=_code(enrollment.secret))
    assert len(activation.recovery_codes) == get_settings().mfa_recovery_code_count
    assert activation.pair.mfa_verified is True
    claims = decode_access_token(get_settings(), activation.pair.access_token)
    assert claims.amr == frozenset({"pwd", "totp"})
    assert claims.token_version == old_version + 1

    state = await _tenant_mfa_state(tenant_id, user_id)
    assert state["mfa_enabled"] is True
    assert state["mfa_secret_enc"] is not None
    assert state["token_version"] == old_version + 1
    assert state["failed_login_attempts"] == 0
    rows = await _refresh_rows(tenant_id)
    assert [row["revoked_reason"] for row in rows] == ["mfa_change", None]
    assert rows[1]["mfa_verified"] is True
    assert not await revocation.session_exists(old_pair.session_id, realm="tenant")
    assert await revocation.session_exists(activation.pair.session_id, realm="tenant")
    assert await revocation.get_mfa_pending_secret(realm="tenant", user_id=user_id) is None

    with pytest.raises(service.AuthError):
        await service.refresh_tokens(refresh_token=old_pair.refresh_token)

    started = await _audit_metadata("auth.mfa.enrollment_started")
    assert started[-1]["method"] == "totp"
    activated = await _audit_metadata("auth.mfa.activated")
    assert activated[-1]["recovery_codes_issued"] == len(activation.recovery_codes)
    assert activated[-1]["sessions_revoked"] is True

    # 已启用后不得重复绑定/激活
    with pytest.raises(service.AuthError) as already:
        await service.enroll_mfa(actor=actor, current_password=TENANT_PASSWORD)
    assert already.value.code == "MFA_ALREADY_ENABLED"
    with pytest.raises(service.AuthError) as again:
        await service.activate_mfa(actor=actor, code=_code(enrollment.secret))
    assert again.value.code == "MFA_ALREADY_ENABLED"


@pytest.mark.asyncio
async def test_activated_recovery_codes_work_for_login_and_are_single_use(
    mfa_env: None, fixed_totp_step: int
) -> None:
    """激活生成的恢复码可完成登录第二步，且每枚只能用一次。"""

    tenant_id, user_id = await _seed_tenant("S-ENR-3")
    enrollment = await service.enroll_mfa(
        actor=_tenant_actor(tenant_id, user_id), current_password=TENANT_PASSWORD
    )
    activation = await service.activate_mfa(
        actor=_tenant_actor(tenant_id, user_id), code=_code(enrollment.secret)
    )

    challenge = await _tenant_login("S-ENR-3")
    pair = await service.verify_mfa(
        mfa_token=challenge.mfa_token, code=activation.recovery_codes[0]
    )
    assert pair.mfa_verified is True
    codes = await _recovery_code_rows(tenant_id, user_id)
    assert len(codes) == get_settings().mfa_recovery_code_count
    assert sum(1 for row in codes if row["used_at"] is not None) == 1

    second = await _tenant_login("S-ENR-3")
    with pytest.raises(service.AuthError) as reused:
        await service.verify_mfa(mfa_token=second.mfa_token, code=activation.recovery_codes[0])
    assert reused.value.code == "INVALID_CREDENTIALS"
    assert (
        await service.verify_mfa(
            mfa_token=second.mfa_token, code=_code(enrollment.secret, fixed_totp_step + 1)
        )
    ).mfa_verified is True


@pytest.mark.asyncio
async def test_activation_fails_when_pending_secret_is_gone(
    mfa_env: None, fixed_totp_step: int
) -> None:
    """待激活项过期/丢失：400 提示重新绑定，不启用 MFA。"""

    tenant_id, user_id = await _seed_tenant("S-ENR-4")
    actor = _tenant_actor(tenant_id, user_id)
    enrollment = await service.enroll_mfa(actor=actor, current_password=TENANT_PASSWORD)
    await revocation.delete_mfa_pending_secret(realm="tenant", user_id=user_id)

    with pytest.raises(service.AuthError) as expired:
        await service.activate_mfa(actor=actor, code=_code(enrollment.secret))
    assert expired.value.code == "MFA_ENROLL_EXPIRED"
    assert expired.value.http_status == 400
    state = await _tenant_mfa_state(tenant_id, user_id)
    assert state["mfa_enabled"] is False
    assert (await _audit_metadata("auth.mfa.activation_failed"))[-1]["reason"] == "enroll_expired"


@pytest.mark.asyncio
async def test_activation_rejects_stale_enroll_token(mfa_env: None, fixed_totp_step: int) -> None:
    """绑定令牌签发后账号凭证版本变更（改密/停用）→ 旧令牌立即失效。"""

    engine = make_test_engine("admin")
    try:
        await seed_platform_user(engine, account="ops-stale-enroll")
    finally:
        await engine.dispose()

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MFA_REQUIRE_PLATFORM", "true")
        get_settings.cache_clear()
        outcome = await service.login_platform(
            account="ops-stale-enroll", password=TEST_PLATFORM_PASSWORD
        )
        assert isinstance(outcome, service.MfaEnrollmentRequired)
        actor = await _platform_actor_from_enroll_token(outcome.enroll_token)
        enrollment = await service.enroll_mfa(actor=actor, current_password=None)

        user_id = await _platform_user_id("ops-stale-enroll")
        await _bump_platform_token_version(user_id)

        with pytest.raises(service.AuthError) as stale:
            await service.activate_mfa(actor=actor, code=_code(enrollment.secret))
        assert stale.value.code == "MFA_LOGIN_EXPIRED"
        state = await _platform_mfa_state(user_id)
        assert state["mfa_enabled"] is False


async def _bump_platform_token_version(user_id: UUID) -> None:
    """直接自增平台账号 token_version。"""

    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session, session.begin():
            await session.execute(
                text("update platform_users set token_version = token_version + 1 where id = :uid"),
                {"uid": user_id},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_platform_forced_enrollment_chain_issues_mfa_verified_session(
    mfa_env: None, fixed_totp_step: int
) -> None:
    """强制绑定链路：enroll 令牌免密码 → 激活 → 之后登录必须走第二步。"""

    engine = make_test_engine("admin")
    try:
        await seed_platform_user(engine, account="ops-forced")
    finally:
        await engine.dispose()

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MFA_REQUIRE_PLATFORM", "true")
        get_settings.cache_clear()

        outcome = await service.login_platform(
            account="ops-forced", password=TEST_PLATFORM_PASSWORD
        )
        assert isinstance(outcome, service.MfaEnrollmentRequired)
        actor = await _platform_actor_from_enroll_token(outcome.enroll_token)
        enrollment = await service.enroll_mfa(actor=actor, current_password=None)
        activation = await service.activate_mfa(actor=actor, code=_code(enrollment.secret))
        assert activation.pair.mfa_verified is True
        assert await _platform_refresh_rows()

        challenge = await service.login_platform(
            account="ops-forced", password=TEST_PLATFORM_PASSWORD
        )
        assert isinstance(challenge, service.MfaChallenge)
        verified = await service.verify_mfa(
            mfa_token=challenge.mfa_token, code=_code(enrollment.secret, fixed_totp_step + 1)
        )
        assert verified.mfa_verified is True
        refreshed = await service.refresh_tokens(refresh_token=verified.refresh_token)
        assert refreshed.mfa_verified is True


@pytest.mark.asyncio
async def test_disable_mfa_requires_password_and_factor_then_rotates_sessions(
    mfa_env: None, fixed_totp_step: int
) -> None:
    """关闭 MFA：密码/验证码任一不符均拒绝且保留绑定；成功后清空绑定、删除恢复码并轮换会话。"""

    tenant_id, user_id = await _seed_tenant("S-ENR-5")
    actor = _tenant_actor(tenant_id, user_id)
    enrollment = await service.enroll_mfa(actor=actor, current_password=TENANT_PASSWORD)
    activation = await service.activate_mfa(actor=actor, code=_code(enrollment.secret))

    with pytest.raises(service.AuthError) as bad_password:
        await service.disable_mfa(
            principal_user_id=user_id,
            realm="tenant",
            tenant_id=tenant_id,
            current_password="wrong-password-1",
            code=_code(enrollment.secret),
        )
    assert bad_password.value.code == "INVALID_CREDENTIALS"
    assert (await _tenant_mfa_state(tenant_id, user_id))["mfa_enabled"] is True
    assert (await _audit_metadata("auth.mfa.disable_failed"))[-1]["reason"] == "password_mismatch"

    with pytest.raises(service.AuthError) as bad_code:
        await service.disable_mfa(
            principal_user_id=user_id,
            realm="tenant",
            tenant_id=tenant_id,
            current_password=TENANT_PASSWORD,
            code=INVALID_CODE,
        )
    assert bad_code.value.code == "INVALID_CREDENTIALS"

    disabled = await service.disable_mfa(
        principal_user_id=user_id,
        realm="tenant",
        tenant_id=tenant_id,
        current_password=TENANT_PASSWORD,
        code=_code(enrollment.secret, fixed_totp_step + 1),
    )
    assert disabled.mfa_verified is False
    claims = decode_access_token(get_settings(), disabled.access_token)
    assert claims.amr == frozenset({"pwd"})

    state = await _tenant_mfa_state(tenant_id, user_id)
    assert state["mfa_enabled"] is False
    assert state["mfa_secret_enc"] is None
    assert state["mfa_last_used_step"] is None
    assert state["token_version"] == _token_version(activation.pair) + 1
    assert await _recovery_code_rows(tenant_id, user_id) == []
    rows = await _refresh_rows(tenant_id)
    assert [row["revoked_reason"] for row in rows] == ["mfa_change", None]
    assert not await revocation.session_exists(activation.pair.session_id, realm="tenant")
    disabled_audit = await _audit_metadata("auth.mfa.disabled")
    assert disabled_audit[-1]["sessions_revoked"] is True
    assert disabled_audit[-1]["recovery_code_used"] is False

    # 关闭后登录回到仅密码
    normal = await _normal_tenant_login("S-ENR-5")
    assert normal.mfa_verified is False

    with pytest.raises(service.AuthError) as not_enabled:
        await service.disable_mfa(
            principal_user_id=user_id,
            realm="tenant",
            tenant_id=tenant_id,
            current_password=TENANT_PASSWORD,
            code=INVALID_CODE,
        )
    assert not_enabled.value.code == "MFA_NOT_ENABLED"


@pytest.mark.asyncio
async def test_disable_mfa_accepts_recovery_code_but_not_under_platform_policy(
    mfa_env: None, fixed_totp_step: int
) -> None:
    """关闭时可用恢复码代替 TOTP；平台强制策略下禁止关闭（403）。"""

    tenant_id, user_id = await _seed_tenant("S-ENR-6")
    actor = _tenant_actor(tenant_id, user_id)
    enrollment = await service.enroll_mfa(actor=actor, current_password=TENANT_PASSWORD)
    activation = await service.activate_mfa(actor=actor, code=_code(enrollment.secret))

    disabled = await service.disable_mfa(
        principal_user_id=user_id,
        realm="tenant",
        tenant_id=tenant_id,
        current_password=TENANT_PASSWORD,
        code=activation.recovery_codes[3],
    )
    assert disabled.mfa_verified is False
    recovery_audit = await _audit_metadata("auth.mfa.recovery_used")
    assert recovery_audit[-1]["phase"] == "disable"

    engine = make_test_engine("admin")
    try:
        await seed_platform_user(engine, account="ops-no-disable")
    finally:
        await engine.dispose()
    platform_id = await _platform_user_id("ops-no-disable")
    await _enable_platform_mfa(platform_id, generate_totp_secret())

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MFA_REQUIRE_PLATFORM", "true")
        get_settings.cache_clear()
        with pytest.raises(service.AuthError) as blocked:
            await service.disable_mfa(
                principal_user_id=platform_id,
                realm="platform",
                tenant_id=None,
                current_password=TEST_PLATFORM_PASSWORD,
                code=INVALID_CODE,
            )
        assert blocked.value.code == "MFA_POLICY_REQUIRED"
        assert blocked.value.http_status == 403
        assert (await _platform_mfa_state(platform_id))["mfa_enabled"] is True


@pytest.mark.asyncio
async def test_admin_mfa_reset_clears_binding_sessions_and_pending_secret(
    mfa_env: None, fixed_totp_step: int
) -> None:
    """运维重置：清绑定/恢复码/锁定、撤销全部会话并删除 Redis 待激活项，审计脱敏。"""

    engine = make_test_engine("admin")
    try:
        await seed_platform_user(engine, account="ops-cli-reset")
    finally:
        await engine.dispose()
    user_id = await _platform_user_id("ops-cli-reset")
    actor = service.MfaActor(user_id=user_id, realm="platform", tenant_id=None)
    enrollment = await service.enroll_mfa(actor=actor, current_password=TEST_PLATFORM_PASSWORD)
    activation = await service.activate_mfa(actor=actor, code=_code(enrollment.secret))

    # 重置前再开一次绑定，留下待激活 secret
    with pytest.raises(service.AuthError) as busy:
        await service.enroll_mfa(actor=actor, current_password=TEST_PLATFORM_PASSWORD)
    assert busy.value.code == "MFA_ALREADY_ENABLED"
    await revocation.store_mfa_pending_secret(
        "v1.placeholder.placeholder", realm="platform", user_id=user_id, ttl_seconds=60
    )

    result = await platform_admin.reset_platform_mfa(
        account="ops-cli-reset", email="ops-reset@example.com"
    )
    assert result.sessions_revoked == 1
    assert result.email_updated is True

    state = await _platform_mfa_state(user_id)
    assert state["mfa_enabled"] is False
    assert state["mfa_secret_enc"] is None
    assert state["mfa_last_used_step"] is None
    assert state["failed_login_attempts"] == 0
    assert state["locked_until"] is None
    assert state["token_version"] == _token_version(activation.pair) + 1
    async with db_session(platform=True) as session:
        codes = await session.scalar(
            text("select count(*) from platform_mfa_recovery_codes where platform_user_id = :uid"),
            {"uid": user_id},
        )
        email = await session.scalar(
            text("select email from platform_users where id = :uid"), {"uid": user_id}
        )
    assert codes == 0
    assert email == "ops-reset@example.com"
    rows = await _platform_refresh_rows()
    assert [row["revoked_reason"] for row in rows] == ["admin_revoke"]
    assert not await revocation.session_exists(activation.pair.session_id, realm="platform")
    assert await revocation.get_mfa_pending_secret(realm="platform", user_id=user_id) is None

    audit = await _audit_metadata("platform.user.mfa_reset")
    assert audit[-1]["account_masked"] == "o***"
    assert audit[-1]["sessions_revoked"] == 1
    assert audit[-1]["email_updated"] is True

    with pytest.raises(service.AuthError):
        await service.refresh_tokens(refresh_token=activation.pair.refresh_token)
    normal = await service.login_platform(account="ops-cli-reset", password=TEST_PLATFORM_PASSWORD)
    assert isinstance(normal, service.TokenPair)


@pytest.mark.asyncio
async def test_platform_admin_cli_reset_mfa_contract(
    mfa_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 约定：`--reset-mfa` 可免邮箱、不打印密钥；缺参数或账号不存在返回非零。"""

    engine = make_test_engine("admin")
    try:
        await seed_platform_user(engine, account="ops-cli-main")
    finally:
        await engine.dispose()

    assert await platform_admin.run(["--account", "ops-cli-main", "--reset-mfa"]) == 0
    out = capsys.readouterr().out
    assert "ops-cli-main" in out
    assert "otpauth" not in out.lower()

    assert await platform_admin.run(["--account", "ops-cli-main"]) == 1
    assert "必须提供 --email" in capsys.readouterr().err
    assert await platform_admin.run(["--account", "ops-cli-missing", "--reset-mfa"]) == 1
    assert "平台账号不存在" in capsys.readouterr().err
    assert (
        await platform_admin.run(
            [
                "--account",
                "ops-cli-main",
                "--email",
                "a@b.test",
                "--update-email-only",
                "--reset-mfa",
            ]
        )
        == 1
    )
    assert "不能同时使用" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_reset_mfa_rejects_unknown_account(mfa_env: None) -> None:
    """重置只作用于已存在的平台账号（不静默创建）。"""

    with pytest.raises(platform_admin.PlatformAccountProvisionError):
        await platform_admin.reset_platform_mfa(account="ops-none")


@pytest.mark.asyncio
async def test_activation_reports_unavailable_not_expired_when_redis_down(
    mfa_env: None, fixed_totp_step: int
) -> None:
    """Redis 故障时激活报 503「暂不可用」而非 400「绑定已过期」，且不启用 MFA、不签发会话。"""

    from redis.exceptions import RedisError

    tenant_id, user_id = await _seed_tenant("S-MFA-REDIS")
    actor = _tenant_actor(tenant_id, user_id)
    enrollment = await service.enroll_mfa(actor=actor, current_password=TENANT_PASSWORD)

    def _broken_redis() -> None:
        msg = "模拟 Redis 不可用"
        raise RedisError(msg)

    # 单独一个 MonkeyPatch：undo 时不能连带撤销 fixed_totp_step 的时间冻结
    broken = pytest.MonkeyPatch()
    broken.setattr(revocation, "_redis", _broken_redis)
    with pytest.raises(service.AuthError) as unavailable:
        await service.activate_mfa(
            actor=actor, code=_code(enrollment.secret), ip="203.0.113.9", user_agent="pytest"
        )
    assert unavailable.value.code == "MFA_UNAVAILABLE"
    assert unavailable.value.http_status == 503

    broken.undo()
    state = await _tenant_mfa_state(tenant_id, user_id)
    assert state["mfa_enabled"] is False
    assert await _refresh_rows(tenant_id) == []

    # 待激活密钥仍在 TTL 内：恢复后可直接完成激活，无需重新发起绑定
    activation = await service.activate_mfa(
        actor=actor, code=_code(enrollment.secret), ip="203.0.113.9", user_agent="pytest"
    )
    assert activation.pair.mfa_verified is True
