"""M1-D2 集成测试：MFA 登录挑战、TOTP/恢复码校验、锁定与刷新策略（真实 PG + Redis）。"""

import os
from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.auth import revocation, service
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
from tests.support.metrics import counter_value
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
    enable_tenant_mfa as _enable_tenant_mfa,
)
from tests.support.mfa import (
    insert_recovery_codes as _insert_recovery_codes,
)
from tests.support.mfa import (
    mfa_code as _code,
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
from tests.support.mfa import (
    tenant_mfa_user as _tenant_mfa_user,
)

pytestmark = [pytest.mark.integration]


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


@pytest.mark.asyncio
async def test_mfa_challenge_then_totp_verify_with_replay_guard(
    mfa_env: None, fixed_totp_step: int
) -> None:
    """密码通过只发挑战：正确 TOTP 才签会话；错误计入锁定；同一时间步不可重放。"""

    tenant_id, user_id, secret = await _tenant_mfa_user("S-MFA-1")

    challenge = await _tenant_login("S-MFA-1")
    assert challenge.expires_in == get_settings().mfa_challenge_ttl_seconds
    assert await _refresh_rows(tenant_id) == []
    assert await _audit_metadata("auth.login.success") == []

    mfa_failures_before = counter_value(
        "campus_auth_login_failures_total", {"realm": "tenant", "reason": "invalid_mfa_code"}
    )
    with pytest.raises(service.AuthError) as wrong:
        await service.verify_mfa(mfa_token=challenge.mfa_token, code=INVALID_CODE)
    assert wrong.value.code == "INVALID_CREDENTIALS"
    assert (
        counter_value(
            "campus_auth_login_failures_total", {"realm": "tenant", "reason": "invalid_mfa_code"}
        )
        == mfa_failures_before + 1
    )

    state = await _tenant_mfa_state(tenant_id, user_id)
    assert state["failed_login_attempts"] == 1
    failed = await _audit_metadata("auth.login.failed")
    assert failed[-1]["reason"] == "invalid_mfa_code"
    assert failed[-1]["mfa"] is True

    pair = await service.verify_mfa(mfa_token=challenge.mfa_token, code=_code(secret))
    assert pair.mfa_verified is True
    claims = decode_access_token(get_settings(), pair.access_token)
    assert claims.amr == frozenset({"pwd", "totp"})

    rows = await _refresh_rows(tenant_id)
    assert len(rows) == 1 and rows[0]["mfa_verified"] is True
    state = await _tenant_mfa_state(tenant_id, user_id)
    assert state["mfa_last_used_step"] == fixed_totp_step
    assert state["failed_login_attempts"] == 0
    assert state["locked_until"] is None
    success = await _audit_metadata("auth.login.success")
    assert success[-1]["mfa"] is True
    assert success[-1]["recovery_code_used"] is False

    # 挑战成功即消费：同一 mfa_token 不能再次使用
    with pytest.raises(service.AuthError) as consumed:
        await service.verify_mfa(mfa_token=challenge.mfa_token, code=_code(secret))
    assert consumed.value.code == "MFA_LOGIN_EXPIRED"

    # 新挑战中重放同一步码失败；下一步码可用（窗口 ±1）
    second = await _tenant_login("S-MFA-1")
    with pytest.raises(service.AuthError) as replay:
        await service.verify_mfa(mfa_token=second.mfa_token, code=_code(secret))
    assert replay.value.code == "INVALID_CREDENTIALS"

    accepted = await service.verify_mfa(
        mfa_token=second.mfa_token, code=_code(secret, fixed_totp_step + 1)
    )
    assert accepted.mfa_verified is True


@pytest.mark.asyncio
async def test_recovery_code_login_is_single_use(mfa_env: None, fixed_totp_step: int) -> None:
    """恢复码可替代 TOTP 登录，大小写/分隔符容错，且只能使用一次并留审计。"""

    tenant_id, user_id, _ = await _tenant_mfa_user("S-MFA-2")
    codes = await _insert_recovery_codes(tenant_id, user_id)

    challenge = await _tenant_login("S-MFA-2")
    pair = await service.verify_mfa(
        mfa_token=challenge.mfa_token, code=codes[0].lower().replace("-", " ")
    )
    assert pair.mfa_verified is True

    async with db_session(tenant_id=tenant_id) as session:
        used = (
            await session.execute(
                text(
                    "select count(*) from mfa_recovery_codes "
                    "where tenant_id = :tid and user_id = :uid and used_at is not null"
                ),
                {"tid": tenant_id, "uid": user_id},
            )
        ).scalar()
    assert used == 1
    recovery_audit = await _audit_metadata("auth.mfa.recovery_used")
    assert recovery_audit[-1]["phase"] == "login"

    # 同一恢复码不能再次使用；另一枚仍可用
    second = await _tenant_login("S-MFA-2")
    with pytest.raises(service.AuthError):
        await service.verify_mfa(mfa_token=second.mfa_token, code=codes[0])
    accepted = await service.verify_mfa(mfa_token=second.mfa_token, code=codes[1])
    assert accepted.mfa_verified is True


@pytest.mark.asyncio
async def test_mfa_failures_lock_account(mfa_env: None, fixed_totp_step: int) -> None:
    """MFA 错误与密码错误共用锁定策略：达到阈值后密码正确也无法登录。"""

    tenant_id, user_id, _ = await _tenant_mfa_user("S-MFA-3")
    settings = get_settings()

    challenge = await _tenant_login("S-MFA-3")
    failures_before = counter_value(
        "campus_auth_login_failures_total", {"realm": "tenant", "reason": "invalid_mfa_code"}
    )
    lockouts_before = counter_value("campus_auth_account_lockouts_total", {"realm": "tenant"})
    for _ in range(settings.auth_max_login_failures):
        with pytest.raises(service.AuthError):
            await service.verify_mfa(mfa_token=challenge.mfa_token, code=INVALID_CODE)

    assert (
        counter_value(
            "campus_auth_login_failures_total", {"realm": "tenant", "reason": "invalid_mfa_code"}
        )
        == failures_before + settings.auth_max_login_failures
    )
    # MFA 失败与密码失败共用锁定：达到阈值的这一次记一条锁定，之前的不记
    assert (
        counter_value("campus_auth_account_lockouts_total", {"realm": "tenant"})
        == lockouts_before + 1
    )

    async with db_session(tenant_id=tenant_id) as session:
        locked_until = await session.scalar(
            text("select locked_until from users where id = :uid"), {"uid": user_id}
        )
    assert locked_until is not None
    failed = await _audit_metadata("auth.login.failed")
    assert failed[-1]["failed_attempts"] == settings.auth_max_login_failures
    assert failed[-1]["lock_applied"] is True

    with pytest.raises(service.AuthError) as locked:
        await service.login_tenant(
            school_code=SCHOOL_CODE, login_id="S-MFA-3", password=TENANT_PASSWORD
        )
    assert locked.value.code == "INVALID_CREDENTIALS"
    assert await _refresh_rows(tenant_id) == []
    # 锁定期间的尝试只记失败（reason=locked），不重复记"新锁定"
    assert (
        counter_value(
            "campus_auth_login_failures_total", {"realm": "tenant", "reason": "locked"}
        )
        >= 1
    )
    assert (
        counter_value("campus_auth_account_lockouts_total", {"realm": "tenant"})
        == lockouts_before + 1
    )


@pytest.mark.asyncio
async def test_stale_challenge_rejected_after_token_version_change(
    mfa_env: None, fixed_totp_step: int
) -> None:
    """改密/MFA 变更后旧挑战立即失效（token_version 不匹配）。"""

    tenant_id, user_id, secret = await _tenant_mfa_user("S-MFA-4")
    challenge = await _tenant_login("S-MFA-4")

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

    with pytest.raises(service.AuthError) as stale:
        await service.verify_mfa(mfa_token=challenge.mfa_token, code=_code(secret))
    assert stale.value.code == "MFA_LOGIN_EXPIRED"
    assert await _refresh_rows(tenant_id) == []


@pytest.mark.asyncio
async def test_secret_decrypt_failure_fails_closed(mfa_env: None, fixed_totp_step: int) -> None:
    """密文与账号 AAD 不匹配：500 fail-closed，不签发会话并写审计。"""

    tenant_id, user_id = await _seed_tenant("S-MFA-5")
    secret = generate_totp_secret()
    await _enable_tenant_mfa(tenant_id, user_id, secret, aad_user_id=uuid4())

    challenge = await _tenant_login("S-MFA-5")
    with pytest.raises(service.AuthError) as invalid:
        await service.verify_mfa(mfa_token=challenge.mfa_token, code=_code(secret))
    assert invalid.value.code == "MFA_UNAVAILABLE"
    assert invalid.value.http_status == 500
    assert len(await _audit_metadata("auth.mfa.secret_invalid")) == 1
    assert await _refresh_rows(tenant_id) == []


@pytest.mark.asyncio
async def test_platform_mfa_policy_requires_enrollment(mfa_env: None) -> None:
    """平台强制策略：未绑定返回 enroll_token，已绑定返回挑战；关闭策略恢复原登录。"""

    engine = make_test_engine("admin")
    try:
        await seed_platform_user(engine, account="ops-mfa-unbound")
        await seed_platform_user(engine, account="ops-mfa-bound")
    finally:
        await engine.dispose()
    await _enable_platform_mfa(await _platform_user_id("ops-mfa-bound"), generate_totp_secret())

    get_settings.cache_clear()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MFA_REQUIRE_PLATFORM", "true")
        get_settings.cache_clear()

        unbound = await service.login_platform(
            account="ops-mfa-unbound", password=TEST_PLATFORM_PASSWORD
        )
        assert isinstance(unbound, service.MfaEnrollmentRequired)
        bound = await service.login_platform(
            account="ops-mfa-bound", password=TEST_PLATFORM_PASSWORD
        )
        assert isinstance(bound, service.MfaChallenge)

        async with db_session(platform=True) as session:
            count = await session.scalar(text("select count(*) from platform_refresh_tokens"))
        assert count == 0

        get_settings.cache_clear()
        patch.setenv("MFA_REQUIRE_PLATFORM", "false")
        get_settings.cache_clear()
        normal = await service.login_platform(
            account="ops-mfa-unbound", password=TEST_PLATFORM_PASSWORD
        )
        assert isinstance(normal, service.TokenPair)
        assert normal.mfa_verified is False


@pytest.mark.asyncio
async def test_platform_refresh_requires_mfa_when_policy_enabled(
    mfa_env: None, fixed_totp_step: int
) -> None:
    """策略开启后未完成 MFA 的刷新会话被撤销（mfa_change）；完成 MFA 的会话可续期。"""

    engine = make_test_engine("admin")
    try:
        await seed_platform_user(engine, account="ops-mfa-refresh")
    finally:
        await engine.dispose()

    unverified = await service.login_platform(
        account="ops-mfa-refresh", password=TEST_PLATFORM_PASSWORD
    )
    assert isinstance(unverified, service.TokenPair)

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MFA_REQUIRE_PLATFORM", "true")
        get_settings.cache_clear()

        with pytest.raises(service.AuthError) as required:
            await service.refresh_tokens(refresh_token=unverified.refresh_token)
        assert required.value.code == "MFA_REQUIRED"

        platform_rows = await _platform_refresh_rows()
        assert [row["revoked_reason"] for row in platform_rows] == ["mfa_change"]
        # Redis 会话标记随撤销删除：访问令牌下一次请求立即失效
        assert not await revocation.session_exists(unverified.session_id, realm="platform")

    # 关闭策略后走完整 MFA 登录，刷新保留 amr 与 mfa_verified
    get_settings.cache_clear()
    user_id = await _platform_user_id("ops-mfa-refresh")
    secret = generate_totp_secret()
    await _enable_platform_mfa(user_id, secret)

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MFA_REQUIRE_PLATFORM", "true")
        get_settings.cache_clear()

        challenge = await service.login_platform(
            account="ops-mfa-refresh", password=TEST_PLATFORM_PASSWORD
        )
        assert isinstance(challenge, service.MfaChallenge)
        pair = await service.verify_mfa(mfa_token=challenge.mfa_token, code=_code(secret))
        assert pair.mfa_verified is True

        refreshed = await service.refresh_tokens(refresh_token=pair.refresh_token)
        assert refreshed.mfa_verified is True
        claims = decode_access_token(get_settings(), refreshed.access_token)
        assert claims.amr == frozenset({"pwd", "totp"})


@pytest.mark.asyncio
async def test_change_password_preserves_mfa_amr(mfa_env: None, fixed_totp_step: int) -> None:
    """改密重签时保留当前会话的 MFA 完成状态，不降级为仅密码会话。"""

    tenant_id, user_id, secret = await _tenant_mfa_user("S-MFA-6")
    challenge = await _tenant_login("S-MFA-6")
    pair = await service.verify_mfa(mfa_token=challenge.mfa_token, code=_code(secret))

    new_pair = await service.change_password(
        principal_user_id=user_id,
        realm="tenant",
        tenant_id=tenant_id,
        current_password=TENANT_PASSWORD,
        new_password="Brand-New-Pass-1",
        mfa_verified=pair.mfa_verified,
    )
    assert new_pair.mfa_verified is True
    claims = decode_access_token(get_settings(), new_pair.access_token)
    assert claims.amr == frozenset({"pwd", "totp"})

    rows = await _refresh_rows(tenant_id)
    latest = [row for row in rows if row["revoked_reason"] is None]
    assert len(latest) == 1 and latest[0]["mfa_verified"] is True


@pytest.mark.asyncio
async def test_verify_fails_closed_when_redis_unavailable(
    mfa_env: None, fixed_totp_step: int
) -> None:
    """Redis 故障时 MFA 第二步不得放行：503、不签发会话、不消费挑战、不计失败次数。"""

    from redis.exceptions import RedisError

    tenant_id, user_id, secret = await _tenant_mfa_user("S-MFA-7")
    challenge = await _tenant_login("S-MFA-7")

    def _broken_redis() -> None:
        msg = "模拟 Redis 不可用"
        raise RedisError(msg)

    # 单独一个 MonkeyPatch：undo 时不能连带撤销 fixed_totp_step 的时间冻结
    broken = pytest.MonkeyPatch()
    broken.setattr(revocation, "_redis", _broken_redis)
    with pytest.raises(service.AuthError) as unavailable:
        await service.verify_mfa(mfa_token=challenge.mfa_token, code=_code(secret))
    assert unavailable.value.code == "MFA_UNAVAILABLE"
    assert unavailable.value.http_status == 503
    assert await _refresh_rows(tenant_id) == []

    async with db_session(tenant_id=tenant_id) as session:
        state = (
            (
                await session.execute(
                    text(
                        "select failed_login_attempts, mfa_last_used_step from users where id = :uid"
                    ),
                    {"uid": user_id},
                )
            )
            .mappings()
            .one()
        )
    assert state["failed_login_attempts"] == 0
    assert state["mfa_last_used_step"] is None

    # Redis 恢复后同一挑战仍可兑换（故障不消耗用户的登录状态）
    broken.undo()
    pair = await service.verify_mfa(mfa_token=challenge.mfa_token, code=_code(secret))
    assert pair.mfa_verified is True
    assert len(await _refresh_rows(tenant_id)) == 1


@pytest.mark.asyncio
async def test_login_fails_closed_when_challenge_cannot_be_registered(
    mfa_env: None, fixed_totp_step: int
) -> None:
    """Redis 故障导致挑战无法登记时，登录不得签发一个"永远验证不了"的挑战：503 且不建会话。"""

    from redis.exceptions import RedisError

    tenant_id, _user_id, _secret = await _tenant_mfa_user("S-MFA-8")

    def _broken_redis() -> None:
        msg = "模拟 Redis 不可用"
        raise RedisError(msg)

    broken = pytest.MonkeyPatch()
    broken.setattr(revocation, "_redis", _broken_redis)
    with pytest.raises(service.AuthError) as unavailable:
        await service.login_tenant(
            school_code=SCHOOL_CODE, login_id="S-MFA-8", password=TENANT_PASSWORD
        )
    assert unavailable.value.code == "MFA_UNAVAILABLE"
    assert unavailable.value.http_status == 503
    # 没有签发任何会话
    assert await _refresh_rows(tenant_id) == []

    # Redis 恢复后可正常进入第二步
    broken.undo()
    challenge = await _tenant_login("S-MFA-8")
    assert challenge.mfa_token
