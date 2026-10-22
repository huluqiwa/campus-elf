"""M1-D1 集成测试：登录失败锁定、密码策略/历史与改密副作用（真实 PG + Redis）。"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth import revocation, service
from app.config import get_settings
from app.db.session import db_session, dispose_engine
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

pytestmark = [pytest.mark.integration, pytest.mark.business]

SCHOOL_CODE = "SCH-D1TEST1"
TENANT_PASSWORD = "Tenant-Test-Pass-1"


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
async def auth_env(migrated_database: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """指向测试库/Redis，并清空业务数据。"""

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

    yield

    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()


@pytest.fixture
async def admin_engine(migrated_database: None) -> AsyncIterator[AsyncEngine]:
    """管理连接引擎（种子/修改锁定字段）。"""

    engine = make_test_engine("admin")
    yield engine
    await engine.dispose()


async def _user_row(tenant_id: UUID, user_id: UUID) -> dict[str, object]:
    async with db_session(tenant_id=tenant_id) as session:
        row = (
            (
                await session.execute(
                    text(
                        "select failed_login_attempts, failed_login_window_started_at, "
                        "locked_until from users where id = :uid"
                    ),
                    {"uid": user_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _failed_reasons() -> list[str]:
    async with db_session(platform=True) as session:
        values: list[object] = list(
            (
                await session.execute(
                    text(
                        "select metadata_json->>'reason' from audit_logs "
                        "where action = 'auth.login.failed' order by created_at, event_id"
                    )
                )
            )
            .scalars()
            .all()
        )
    return [str(value) for value in values]


async def _change_failed_reasons() -> list[str]:
    async with db_session(platform=True) as session:
        values: list[object] = list(
            (
                await session.execute(
                    text(
                        "select metadata_json->>'reason' from audit_logs "
                        "where action = 'auth.password.change_failed' "
                        "order by created_at, event_id"
                    )
                )
            )
            .scalars()
            .all()
        )
    return [str(value) for value in values]


@pytest.mark.asyncio
async def test_tenant_login_locks_after_five_failures(
    auth_env: None, admin_engine: AsyncEngine
) -> None:
    """连续 5 次失败 → 锁定；锁定期间正确密码也返回统一 401；过期后成功并清零。"""

    tenant_id, user_id = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-3001",
        password=TENANT_PASSWORD,
        email="admin@school.test",
    )

    for _ in range(5):
        with pytest.raises(service.AuthError) as failure:
            await service.login_tenant(
                school_code=SCHOOL_CODE, login_id="S-3001", password="wrong-password"
            )
        assert failure.value.message == service.INVALID_CREDENTIALS_MESSAGE
        assert failure.value.http_status == 401

    row = await _user_row(tenant_id, user_id)
    assert row["failed_login_attempts"] == 5
    assert row["locked_until"] is not None

    with pytest.raises(service.AuthError):
        await service.login_tenant(
            school_code=SCHOOL_CODE, login_id="S-3001", password=TENANT_PASSWORD
        )
    assert (await _failed_reasons())[-1] == "locked"

    # 模拟锁定到期：成功后计数与锁定字段清零
    async with admin_engine.begin() as conn:
        await conn.execute(
            text("update users set locked_until = now() - interval '1 second' where id = :uid"),
            {"uid": user_id},
        )

    pair = as_token_pair(
        await service.login_tenant(
            school_code=SCHOOL_CODE, login_id="S-3001", password=TENANT_PASSWORD
        )
    )
    assert pair.user_id == user_id
    row = await _user_row(tenant_id, user_id)
    assert row["failed_login_attempts"] == 0
    assert row["failed_login_window_started_at"] is None
    assert row["locked_until"] is None


@pytest.mark.asyncio
async def test_failure_window_expiry_resets_counter(
    auth_env: None, admin_engine: AsyncEngine
) -> None:
    """失败窗口过期后重新计数（不累计跨窗口失败）。"""

    tenant_id, user_id = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-3002",
        password=TENANT_PASSWORD,
    )

    for _ in range(4):
        with pytest.raises(service.AuthError):
            await service.login_tenant(
                school_code=SCHOOL_CODE, login_id="S-3002", password="wrong-password"
            )
    assert (await _user_row(tenant_id, user_id))["failed_login_attempts"] == 4

    async with admin_engine.begin() as conn:
        await conn.execute(
            text(
                "update users set failed_login_window_started_at = "
                "now() - interval '16 minutes' where id = :uid"
            ),
            {"uid": user_id},
        )

    with pytest.raises(service.AuthError):
        await service.login_tenant(
            school_code=SCHOOL_CODE, login_id="S-3002", password="wrong-password"
        )
    row = await _user_row(tenant_id, user_id)
    assert row["failed_login_attempts"] == 1
    assert row["locked_until"] is None


@pytest.mark.asyncio
async def test_platform_login_lockout(auth_env: None, admin_engine: AsyncEngine) -> None:
    """平台账号同样 5 次失败锁定，第 6 次正确密码仍拒绝，审计记录 locked。"""

    await seed_platform_user(admin_engine, account="ops-d1-lock", email="ops@example.test")

    for _ in range(5):
        with pytest.raises(service.AuthError):
            await service.login_platform(account="ops-d1-lock", password="wrong-password")

    with pytest.raises(service.AuthError):
        await service.login_platform(account="ops-d1-lock", password=TEST_PLATFORM_PASSWORD)
    assert (await _failed_reasons())[-1] == "locked"

    async with db_session(platform=True) as session:
        row = (
            (
                await session.execute(
                    text(
                        "select failed_login_attempts, locked_until from platform_users "
                        "where account = 'ops-d1-lock'"
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["failed_login_attempts"] == 5
    assert row["locked_until"] is not None


@pytest.mark.asyncio
async def test_concurrent_login_failures_are_serialized(
    auth_env: None, admin_engine: AsyncEngine
) -> None:
    """并发错误登录不丢计数：最终恰好 5 次计入，其余落在 locked 原因。"""

    tenant_id, user_id = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-3005",
        password=TENANT_PASSWORD,
    )

    results = await asyncio.gather(
        *[
            service.login_tenant(
                school_code=SCHOOL_CODE, login_id="S-3005", password="wrong-password"
            )
            for _ in range(6)
        ],
        return_exceptions=True,
    )
    assert all(isinstance(item, service.AuthError) for item in results)

    row = await _user_row(tenant_id, user_id)
    assert row["failed_login_attempts"] == 5
    assert row["locked_until"] is not None

    reasons = await _failed_reasons()
    assert reasons.count("invalid_password") == 5
    assert reasons.count("locked") == 1


@pytest.mark.asyncio
async def test_change_password_failures_count_toward_lockout(
    auth_env: None, admin_engine: AsyncEngine
) -> None:
    """改密时连续输错当前密码同样累计失败并触发锁定，避免绕过 AUTH-05。"""

    tenant_id, user_id = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-3006",
        password=TENANT_PASSWORD,
    )
    new_password = "Changed-Pass-77"

    for _ in range(5):
        with pytest.raises(service.AuthError) as failure:
            await service.change_password(
                principal_user_id=user_id,
                realm="tenant",
                tenant_id=tenant_id,
                current_password="wrong-current",
                new_password=new_password,
            )
        assert failure.value.http_status == 401
        assert failure.value.message == "当前密码不正确"

    row = await _user_row(tenant_id, user_id)
    assert row["failed_login_attempts"] == 5
    assert row["locked_until"] is not None

    with pytest.raises(service.AuthError) as locked:
        await service.change_password(
            principal_user_id=user_id,
            realm="tenant",
            tenant_id=tenant_id,
            current_password=TENANT_PASSWORD,
            new_password=new_password,
        )
    assert locked.value.message == "当前密码不正确"

    reasons = await _change_failed_reasons()
    assert reasons.count("password_mismatch") == 5
    assert reasons.count("locked") == 1

    with pytest.raises(service.AuthError):
        await service.login_tenant(
            school_code=SCHOOL_CODE, login_id="S-3006", password=TENANT_PASSWORD
        )
    assert (await _failed_reasons())[-1] == "locked"


@pytest.mark.asyncio
async def test_change_password_success_clears_failure_counter(
    auth_env: None, admin_engine: AsyncEngine
) -> None:
    """4 次失败后提供正确当前密码并改密成功：计数与窗口清零。"""

    tenant_id, user_id = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-3007",
        password=TENANT_PASSWORD,
    )

    for _ in range(4):
        with pytest.raises(service.AuthError):
            await service.change_password(
                principal_user_id=user_id,
                realm="tenant",
                tenant_id=tenant_id,
                current_password="wrong-current",
                new_password="Changed-Pass-88",
            )
    assert (await _user_row(tenant_id, user_id))["failed_login_attempts"] == 4

    await service.change_password(
        principal_user_id=user_id,
        realm="tenant",
        tenant_id=tenant_id,
        current_password=TENANT_PASSWORD,
        new_password="Changed-Pass-88",
    )
    row = await _user_row(tenant_id, user_id)
    assert row["failed_login_attempts"] == 0
    assert row["failed_login_window_started_at"] is None
    assert row["locked_until"] is None


@pytest.mark.asyncio
async def test_change_password_policy_history_and_lockout_clear(
    auth_env: None, admin_engine: AsyncEngine
) -> None:
    """改密：弱密码/含登录名/复用旧密码被拒；成功后写历史并清除锁定字段。"""

    tenant_id, user_id = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-3003",
        password=TENANT_PASSWORD,
    )
    await service.login_tenant(school_code=SCHOOL_CODE, login_id="S-3003", password=TENANT_PASSWORD)

    with pytest.raises(service.AuthError) as weak:
        await service.change_password(
            principal_user_id=user_id,
            realm="tenant",
            tenant_id=tenant_id,
            current_password=TENANT_PASSWORD,
            new_password="short1A",
        )
    assert weak.value.code == "WEAK_PASSWORD"
    assert weak.value.http_status == 422

    with pytest.raises(service.AuthError) as contains_login:
        await service.change_password(
            principal_user_id=user_id,
            realm="tenant",
            tenant_id=tenant_id,
            current_password=TENANT_PASSWORD,
            new_password="XxS-3003-Abc1",
        )
    assert contains_login.value.code == "WEAK_PASSWORD"

    new_password = "Brand-New-Pass-1"
    await service.change_password(
        principal_user_id=user_id,
        realm="tenant",
        tenant_id=tenant_id,
        current_password=TENANT_PASSWORD,
        new_password=new_password,
    )

    async with db_session(tenant_id=tenant_id) as session:
        history: list[object] = list(
            (
                await session.execute(
                    text(
                        "select password_hash from password_history "
                        "where user_id = :uid order by created_at desc"
                    ),
                    {"uid": user_id},
                )
            )
            .scalars()
            .all()
        )
    assert len(history) == 1
    from app.auth.passwords import verify_password

    assert verify_password(TENANT_PASSWORD, str(history[0]))

    with pytest.raises(service.AuthError) as reuse:
        await service.change_password(
            principal_user_id=user_id,
            realm="tenant",
            tenant_id=tenant_id,
            current_password=new_password,
            new_password=TENANT_PASSWORD,
        )
    assert reuse.value.code == "WEAK_PASSWORD"
    assert "最近" in reuse.value.message

    with pytest.raises(service.AuthError) as same:
        await service.change_password(
            principal_user_id=user_id,
            realm="tenant",
            tenant_id=tenant_id,
            current_password=new_password,
            new_password=new_password,
        )
    assert same.value.code == "WEAK_PASSWORD"

    # 锁定字段在改密成功后清零（成功改密会清除计数与窗口）
    async with admin_engine.begin() as conn:
        await conn.execute(
            text(
                "update users set failed_login_attempts = 4, "
                "failed_login_window_started_at = now() where id = :uid"
            ),
            {"uid": user_id},
        )

    await service.change_password(
        principal_user_id=user_id,
        realm="tenant",
        tenant_id=tenant_id,
        current_password=new_password,
        new_password="Brand-New-Pass-2",
    )
    row = await _user_row(tenant_id, user_id)
    assert row["failed_login_attempts"] == 0
    assert row["failed_login_window_started_at"] is None
    assert row["locked_until"] is None


@pytest.mark.asyncio
async def test_password_history_is_capped(auth_env: None, admin_engine: AsyncEngine) -> None:
    """历史只保留最近 N 条：插入第 N+1 条时最旧记录被裁剪。"""

    tenant_id, user_id = await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_CODE,
        login_id="S-3004",
        password=TENANT_PASSWORD,
    )
    await service.login_tenant(school_code=SCHOOL_CODE, login_id="S-3004", password=TENANT_PASSWORD)

    # 直接预置 5 条真实历史（等价于最近 5 个旧密码）
    from app.auth.passwords import hash_password

    async with admin_engine.begin() as conn:
        for index in range(5):
            await conn.execute(
                text(
                    "insert into password_history (tenant_id, user_id, password_hash) "
                    "values (:tid, :uid, :hash)"
                ),
                {
                    "tid": tenant_id,
                    "uid": user_id,
                    "hash": hash_password(f"Old-Password-{index}"),
                },
            )

    await service.change_password(
        principal_user_id=user_id,
        realm="tenant",
        tenant_id=tenant_id,
        current_password=TENANT_PASSWORD,
        new_password="Brand-New-Pass-3",
    )

    async with db_session(tenant_id=tenant_id) as session:
        count = await session.scalar(
            text("select count(*) from password_history where user_id = :uid"),
            {"uid": user_id},
        )
    assert count == 5

    # 锁定/窗口字段未被本次改密之外的数据污染
    row = await _user_row(tenant_id, user_id)
    assert row["failed_login_attempts"] == 0


def test_lockout_state_uses_utc_now() -> None:
    """防止误用本地时间：锁定截止与当前时间都按 UTC 比较。"""

    from app.auth.lockout import is_account_locked

    now = datetime.now(UTC)
    assert is_account_locked(now + timedelta(seconds=1), now) is True
    assert is_account_locked(now - timedelta(seconds=1), now) is False
