"""M1-C2 集成测试：会话限额、踢出、设备列表、下线、退出其它与 Redis 标记（真实 PG + Redis）。"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.auth import principal as principal_module
from app.auth import revocation, service, sessions
from app.auth.principal import get_current_principal
from app.auth.tokens import decode_access_token
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from tests.support.auth import (
    TEST_PLATFORM_PASSWORD,
    as_token_pair,
    seed_platform_user,
)
from tests.support.db import (
    clean_test_data,
    database_url,
    make_session_factory,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.integration]

TENANT_PASSWORD = "Session-Test-Pass-1"
NEW_PASSWORD = "Session-New-Pass-2"
CHROME_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
SAFARI_IOS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
FIREFOX_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) "
    "Gecko/20100101 Firefox/120.0"
)


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
async def session_env(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """指向测试库/Redis，清空业务数据并固定限额配置。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
    finally:
        await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    monkeypatch.setenv("MAX_SESSIONS_PER_USER_TENANT", "3")
    monkeypatch.setenv("MAX_SESSIONS_PER_USER_PLATFORM", "2")
    get_settings.cache_clear()
    await revocation.reset_client()

    yield

    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()


async def _seed_tenant_users(
    login_ids: tuple[str, ...], *, password: str = TENANT_PASSWORD
) -> tuple[UUID, dict[str, UUID], str]:
    """写入一个租户与若干用户，返回 (tenant_id, login_id→user_id, school_code)。"""

    from app.auth.passwords import hash_password

    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session, session.begin():
            school_code = f"SCH-SESS{uuid4().hex[:6].upper()}"
            tenant_id = await session.scalar(
                text(
                    "insert into tenants (school_code, name, status, activated_at) "
                    "values (:code, '会话测试中学', 'active', now()) returning id"
                ),
                {"code": school_code},
            )
            user_ids: dict[str, UUID] = {}
            for index, login_id in enumerate(login_ids):
                user_ids[login_id] = await session.scalar(
                    text(
                        "insert into users (tenant_id, user_type, login_id, display_name, "
                        "password_hash, status, must_change_password, mfa_enabled) "
                        "values (:tenant_id, 'student', :login_id, :name, :hash, 'active', "
                        "false, false) returning id"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "login_id": login_id,
                        "name": f"会话学生{index}",
                        "hash": hash_password(password),
                    },
                )
        return tenant_id, user_ids, school_code
    finally:
        await engine.dispose()


async def _login(
    school_code: str,
    login_id: str,
    *,
    user_agent: str = CHROME_WINDOWS,
    password: str = TENANT_PASSWORD,
) -> service.TokenPair:
    """租户登录（固定 IP，UA 可区分设备）。"""

    return as_token_pair(
        await service.login_tenant(
            school_code=school_code,
            login_id=login_id,
            password=password,
            ip="127.0.0.1",
            user_agent=user_agent,
        )
    )


def _request(path: str = "/api/v1/auth/sessions") -> Request:
    """构造用于依赖测试的最小 Request。"""

    return Request(
        scope={
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
            "state": {},
        }
    )


async def _principal_for(pair: service.TokenPair) -> principal_module.Principal:
    """用访问令牌解析主体（触发 Redis 会话标记检查）。"""

    return await get_current_principal(_request(), f"Bearer {pair.access_token}")


async def _expect_unauthorized(pair: service.TokenPair) -> None:
    """断言该访问令牌下一次请求即 401。"""

    with pytest.raises(HTTPException) as excinfo:
        await _principal_for(pair)
    assert excinfo.value.status_code == 401


async def _audit_rows(action: str) -> list[str]:
    """读取指定审计动作的 metadata JSON 文本。"""

    async with db_session(platform=True) as session:
        return list(
            (
                await session.execute(
                    text(
                        "select metadata_json::text from audit_logs "
                        "where action = :action order by created_at"
                    ),
                    {"action": action},
                )
            )
            .scalars()
            .all()
        )


async def _active_family_count_tenant(tenant_id: UUID, user_id: UUID) -> int:
    """租户用户的活跃 family 数。"""

    async with db_session(tenant_id=tenant_id) as session:
        return int(
            await session.scalar(
                text(
                    "select count(distinct family_id) from refresh_tokens "
                    "where user_id = :uid and revoked_at is null and expires_at > now()"
                ),
                {"uid": user_id},
            )
            or 0
        )


async def _family_reason_tenant(tenant_id: UUID, family_id: UUID) -> str | None:
    """租户 family 的激活行撤销原因。"""

    async with db_session(tenant_id=tenant_id) as session:
        value = await session.scalar(
            text(
                "select revoked_reason from refresh_tokens "
                "where family_id = :fid order by issued_at desc limit 1"
            ),
            {"fid": family_id},
        )
    return str(value) if value is not None else None


async def _active_last_seen_tenant(tenant_id: UUID, family_id: UUID) -> datetime | None:
    """租户 family 激活行的 last_seen_at。"""

    async with db_session(tenant_id=tenant_id) as session:
        result: datetime | None = await session.scalar(
            text(
                "select last_seen_at from refresh_tokens "
                "where family_id = :fid and revoked_at is null"
            ),
            {"fid": family_id},
        )
        return result


async def _latest_device_label_tenant(tenant_id: UUID, family_id: UUID) -> str | None:
    """租户 family 最新一行的设备标签。"""

    async with db_session(tenant_id=tenant_id) as session:
        value = await session.scalar(
            text(
                "select device_label from refresh_tokens "
                "where family_id = :fid order by issued_at desc limit 1"
            ),
            {"fid": family_id},
        )
    return str(value) if value is not None else None


async def _set_last_seen_tenant(
    tenant_id: UUID, family_id: UUID, interval: timedelta
) -> None:
    """把某个 family 的活跃行 last_seen_at 后移，制造确定的"最久未用"顺序。"""

    async with db_session(tenant_id=tenant_id) as session:
        await session.execute(
            text(
                "update refresh_tokens set last_seen_at = now() - cast(:interval as interval) "
                "where family_id = :fid and revoked_at is null"
            ),
            {"fid": family_id, "interval": interval},
        )


async def _expire_family_tenant(tenant_id: UUID, family_id: UUID) -> None:
    """把某个 family 全部行的过期时间移到过去（模拟自然过期）。"""

    async with db_session(tenant_id=tenant_id) as session:
        await session.execute(
            text(
                "update refresh_tokens set expires_at = now() - interval '1 second' "
                "where family_id = :fid"
            ),
            {"fid": family_id},
        )


async def _expire_rotated_rows_tenant(tenant_id: UUID, family_id: UUID) -> None:
    """只让家族内已轮换的旧行过期（活跃行仍有效）。"""

    async with db_session(tenant_id=tenant_id) as session:
        await session.execute(
            text(
                "update refresh_tokens set expires_at = now() - interval '1 second' "
                "where family_id = :fid and revoked_reason = 'rotated'"
            ),
            {"fid": family_id},
        )


async def _family_reason_platform(family_id: UUID) -> str | None:
    """平台 family 的激活行撤销原因。"""

    async with db_session(platform=True) as session:
        value = await session.scalar(
            text(
                "select revoked_reason from platform_refresh_tokens "
                "where family_id = :fid order by issued_at desc limit 1"
            ),
            {"fid": family_id},
        )
    return str(value) if value is not None else None


async def _active_family_count_platform(user_id: UUID) -> int:
    """平台用户的活跃 family 数。"""

    async with db_session(platform=True) as session:
        return int(
            await session.scalar(
                text(
                    "select count(distinct family_id) from platform_refresh_tokens "
                    "where platform_user_id = :uid and revoked_at is null and expires_at > now()"
                ),
                {"uid": user_id},
            )
            or 0
        )


@pytest.mark.asyncio
async def test_limit_kicks_oldest_session_and_audits(session_env: None) -> None:
    """第 4 台登录踢最久未用：被踢方下一请求 401，新设备可用，DB/Redis/审计一致。"""

    tenant_id, users, school = await _seed_tenant_users(("S-4101",))
    user_id = users["S-4101"]
    p1 = await _login(school, "S-4101", user_agent=CHROME_WINDOWS)
    p2 = await _login(school, "S-4101", user_agent=SAFARI_IOS)
    p3 = await _login(school, "S-4101", user_agent=FIREFOX_MAC)

    # 制造确定的"最久未用"顺序：p1 最早，p2 次之，p3 最近
    await _set_last_seen_tenant(tenant_id, p1.session_id, timedelta(hours=2))
    await _set_last_seen_tenant(tenant_id, p2.session_id, timedelta(hours=1))

    assert (await _principal_for(p1)).session_id == p1.session_id
    p4 = await _login(school, "S-4101", user_agent=CHROME_WINDOWS)

    assert await _active_family_count_tenant(tenant_id, user_id) == 3
    assert await _family_reason_tenant(tenant_id, p1.session_id) == "session_kicked"
    assert await revocation.session_exists(p1.session_id, realm="tenant") is False
    assert await revocation.session_exists(p4.session_id, realm="tenant") is True

    await _expect_unauthorized(p1)
    # 被踢设备的刷新令牌按"已撤销"拒绝，不得误报重用（简报 §4 第 7 条）
    with pytest.raises(service.AuthError) as excinfo:
        await service.refresh_tokens(refresh_token=p1.refresh_token)
    assert excinfo.value.code == "INVALID_REFRESH_TOKEN"
    assert await _audit_rows("auth.refresh.reuse_detected") == []
    assert (await _principal_for(p2)).session_id == p2.session_id
    assert (await _principal_for(p3)).session_id == p3.session_id
    assert (await _principal_for(p4)).session_id == p4.session_id

    kicked = await _audit_rows("auth.session.kicked")
    assert len(kicked) == 1
    assert str(p1.session_id) in kicked[0]
    assert "Chrome / Windows" in kicked[0]


@pytest.mark.asyncio
async def test_list_and_revoke_single_session(session_env: None) -> None:
    """设备列表只含本人与当前标记；下线单台只影响该设备。"""

    tenant_id, users, school = await _seed_tenant_users(("S-4102",))
    user_id = users["S-4102"]
    p1 = await _login(school, "S-4102", user_agent=CHROME_WINDOWS)
    p2 = await _login(school, "S-4102", user_agent=SAFARI_IOS)

    listed = await sessions.list_sessions(
        realm="tenant",
        tenant_id=tenant_id,
        user_id=user_id,
        current_session_id=p2.session_id,
    )
    assert [item.session_id for item in listed] == [p2.session_id, p1.session_id]
    assert listed[0].current is True and listed[1].current is False
    assert {item.device_label for item in listed} == {"Safari / iOS", "Chrome / Windows"}

    revoked = await sessions.revoke_session(
        realm="tenant",
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=p1.session_id,
        current_session_id=p2.session_id,
        request_id="test-revoke-single",
    )
    assert revoked is True
    assert await _family_reason_tenant(tenant_id, p1.session_id) == "session_revoked"
    assert await revocation.session_exists(p1.session_id, realm="tenant") is False
    await _expect_unauthorized(p1)
    with pytest.raises(service.AuthError):
        await service.refresh_tokens(refresh_token=p1.refresh_token)
    assert (await _principal_for(p2)).session_id == p2.session_id

    remaining = await sessions.list_sessions(
        realm="tenant",
        tenant_id=tenant_id,
        user_id=user_id,
        current_session_id=p2.session_id,
    )
    assert [item.session_id for item in remaining] == [p2.session_id]
    assert len(await _audit_rows("auth.session.revoked")) == 1

    # 不存在的会话（含他人/已撤销）一律按未找到处理
    assert (
        await sessions.revoke_session(
            realm="tenant",
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=uuid4(),
            current_session_id=p2.session_id,
        )
        is False
    )

    # 下线当前设备：标记删除，下一次请求 401
    assert (
        await sessions.revoke_session(
            realm="tenant",
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=p2.session_id,
            current_session_id=p2.session_id,
        )
        is True
    )
    assert await revocation.session_exists(p2.session_id, realm="tenant") is False
    await _expect_unauthorized(p2)


@pytest.mark.asyncio
async def test_revoke_others_keeps_current(session_env: None) -> None:
    """退出其它设备只保留当前 family，并写一条汇总审计。"""

    tenant_id, users, school = await _seed_tenant_users(("S-4103",))
    user_id = users["S-4103"]
    p1 = await _login(school, "S-4103", user_agent=CHROME_WINDOWS)
    p2 = await _login(school, "S-4103", user_agent=SAFARI_IOS)
    p3 = await _login(school, "S-4103", user_agent=FIREFOX_MAC)

    count = await sessions.revoke_others(
        realm="tenant",
        tenant_id=tenant_id,
        user_id=user_id,
        current_session_id=p3.session_id,
        request_id="test-revoke-others",
    )
    assert count == 2
    assert await _family_reason_tenant(tenant_id, p1.session_id) == "session_revoked"
    assert await _family_reason_tenant(tenant_id, p2.session_id) == "session_revoked"
    assert await _family_reason_tenant(tenant_id, p3.session_id) is None
    assert await revocation.session_exists(p1.session_id, realm="tenant") is False
    assert await revocation.session_exists(p2.session_id, realm="tenant") is False
    assert await revocation.session_exists(p3.session_id, realm="tenant") is True
    await _expect_unauthorized(p1)
    await _expect_unauthorized(p2)
    assert (await _principal_for(p3)).session_id == p3.session_id

    remaining = await sessions.list_sessions(
        realm="tenant",
        tenant_id=tenant_id,
        user_id=user_id,
        current_session_id=p3.session_id,
    )
    assert [item.session_id for item in remaining] == [p3.session_id]

    audit = await _audit_rows("auth.session.revoked_others")
    assert len(audit) == 1
    assert str(p3.session_id) in audit[0]
    assert '"revoked_count": 2' in audit[0] or '"revoked_count":2' in audit[0]


@pytest.mark.asyncio
async def test_cannot_list_or_revoke_another_users_session(session_env: None) -> None:
    """同租户内也不能查看/下线他人会话。"""

    tenant_id, users, school = await _seed_tenant_users(("S-4104", "S-4105"))
    alice = await _login(school, "S-4104")
    bob = await _login(school, "S-4105")

    alice_list = await sessions.list_sessions(
        realm="tenant",
        tenant_id=tenant_id,
        user_id=users["S-4104"],
        current_session_id=alice.session_id,
    )
    assert [item.session_id for item in alice_list] == [alice.session_id]
    assert (
        await sessions.revoke_session(
            realm="tenant",
            tenant_id=tenant_id,
            user_id=users["S-4104"],
            session_id=bob.session_id,
            current_session_id=alice.session_id,
        )
        is False
    )
    assert await _family_reason_tenant(tenant_id, bob.session_id) is None
    assert (await _principal_for(bob)).session_id == bob.session_id
    denied = await _audit_rows("auth.session.revoke_denied")
    assert len(denied) == 1
    assert str(bob.session_id) in denied[0]


@pytest.mark.asyncio
async def test_cross_tenant_session_is_not_visible(session_env: None) -> None:
    """跨租户：列表不含他租会话，按 family_id 下线也失败（RLS + user 过滤）。"""

    tenant_a, users_a, school_a = await _seed_tenant_users(("S-4106",))
    tenant_b, _users_b, school_b = await _seed_tenant_users(("S-4107",))
    alice = await _login(school_a, "S-4106")
    bob = await _login(school_b, "S-4107")

    listed = await sessions.list_sessions(
        realm="tenant",
        tenant_id=tenant_a,
        user_id=users_a["S-4106"],
        current_session_id=alice.session_id,
    )
    assert [item.session_id for item in listed] == [alice.session_id]
    assert bob.session_id not in {item.session_id for item in listed}
    assert (
        await sessions.revoke_session(
            realm="tenant",
            tenant_id=tenant_a,
            user_id=users_a["S-4106"],
            session_id=bob.session_id,
            current_session_id=alice.session_id,
        )
        is False
    )
    assert await _family_reason_tenant(tenant_b, bob.session_id) is None
    assert (await _principal_for(bob)).session_id == bob.session_id
    denied = await _audit_rows("auth.session.revoke_denied")
    assert len(denied) == 1
    assert str(bob.session_id) in denied[0]


@pytest.mark.asyncio
async def test_platform_limit_is_stricter(session_env: None) -> None:
    """平台 realm 默认上限 2：第 3 台登录踢最久未用，使用平台表。"""

    engine = make_test_engine("admin")
    try:
        await seed_platform_user(engine, account="ops-sessions")
    finally:
        await engine.dispose()

    p1 = as_token_pair(
        await service.login_platform(
            account="ops-sessions", password=TEST_PLATFORM_PASSWORD, user_agent=CHROME_WINDOWS
        )
    )
    p2 = as_token_pair(
        await service.login_platform(
            account="ops-sessions", password=TEST_PLATFORM_PASSWORD, user_agent=SAFARI_IOS
        )
    )
    p3 = as_token_pair(
        await service.login_platform(
            account="ops-sessions", password=TEST_PLATFORM_PASSWORD, user_agent=FIREFOX_MAC
        )
    )

    assert await _active_family_count_platform(p1.user_id) == 2
    assert await _family_reason_platform(p1.session_id) == "session_kicked"
    assert await revocation.session_exists(p1.session_id, realm="platform") is False
    assert await revocation.session_exists(p3.session_id, realm="platform") is True
    await _expect_unauthorized(p1)
    assert (await _principal_for(p3)).session_id == p3.session_id

    listed = await sessions.list_sessions(
        realm="platform",
        tenant_id=None,
        user_id=p3.user_id,
        current_session_id=p3.session_id,
    )
    assert {item.session_id for item in listed} == {p2.session_id, p3.session_id}
    assert any(item.current for item in listed)


@pytest.mark.asyncio
async def test_refresh_updates_last_seen_and_extends_session_marker(session_env: None) -> None:
    """刷新同一 family：last_seen_at 前移、标记保留、sid 不变。"""

    tenant_id, users, school = await _seed_tenant_users(("S-4108",))
    user_id = users["S-4108"]
    pair = await _login(school, "S-4108")
    await _set_last_seen_tenant(tenant_id, pair.session_id, timedelta(hours=1))
    before = await _active_last_seen_tenant(tenant_id, pair.session_id)
    # 人为把标记压到临近过期，验证刷新会续期到完整 TTL
    await revocation.register_session(pair.session_id, ttl_seconds=5)
    assert 0 < await revocation.session_ttl(pair.session_id, realm="tenant") <= 5

    refreshed = await service.refresh_tokens(refresh_token=pair.refresh_token)
    after = await _active_last_seen_tenant(tenant_id, pair.session_id)

    assert refreshed.session_id == pair.session_id
    assert after is not None and before is not None and after > before
    assert await revocation.session_exists(pair.session_id, realm="tenant") is True
    assert (
        await revocation.session_ttl(pair.session_id, realm="tenant")
        >= get_settings().refresh_token_ttl_seconds - 5
    )
    decoded = decode_access_token(get_settings(), refreshed.access_token)
    assert decoded.session_id == pair.session_id
    assert await _active_family_count_tenant(tenant_id, user_id) == 1


@pytest.mark.asyncio
async def test_refresh_without_user_agent_preserves_device_label(session_env: None) -> None:
    """无 UA 的刷新不得把已有设备标签覆盖成"未知设备"。"""

    tenant_id, _users, school = await _seed_tenant_users(("S-4112",))
    pair = await _login(school, "S-4112", user_agent=CHROME_WINDOWS)
    assert await _latest_device_label_tenant(tenant_id, pair.session_id) == "Chrome / Windows"

    refreshed = await service.refresh_tokens(refresh_token=pair.refresh_token, user_agent=None)
    assert refreshed.session_id == pair.session_id
    assert await _latest_device_label_tenant(tenant_id, pair.session_id) == "Chrome / Windows"


@pytest.mark.asyncio
async def test_change_password_drops_all_session_markers(session_env: None) -> None:
    """改密仍全设备下线：旧 family 标记全部删除，只保留新 family。"""

    tenant_id, users, school = await _seed_tenant_users(("S-4109",))
    user_id = users["S-4109"]
    p1 = await _login(school, "S-4109", user_agent=CHROME_WINDOWS)
    p2 = await _login(school, "S-4109", user_agent=SAFARI_IOS)

    new_pair = await service.change_password(
        principal_user_id=user_id,
        realm="tenant",
        tenant_id=tenant_id,
        current_password=TENANT_PASSWORD,
        new_password=NEW_PASSWORD,
        user_agent=FIREFOX_MAC,
    )

    assert await revocation.session_exists(p1.session_id, realm="tenant") is False
    assert await revocation.session_exists(p2.session_id, realm="tenant") is False
    assert await revocation.session_exists(new_pair.session_id, realm="tenant") is True
    await _expect_unauthorized(p1)
    await _expect_unauthorized(p2)
    assert (await _principal_for(new_pair)).session_id == new_pair.session_id
    assert await _family_reason_tenant(tenant_id, p1.session_id) == "password_change"
    assert await _family_reason_tenant(tenant_id, p2.session_id) == "password_change"


@pytest.mark.asyncio
async def test_logout_drops_current_session_marker(session_env: None) -> None:
    """登出撤销 family 的同时删除会话标记。"""

    _tenant_id, users, school = await _seed_tenant_users(("S-4110",))
    user_id = users["S-4110"]
    pair = await _login(school, "S-4110")
    assert await revocation.session_exists(pair.session_id, realm="tenant") is True

    revoked = await service.revoke_refresh_family(
        refresh_token=pair.refresh_token,
        actor_user_id=user_id,
        actor_realm="tenant",
    )
    assert revoked is not None
    assert revoked.family_id == pair.session_id
    assert await revocation.session_exists(pair.session_id, realm="tenant") is False
    with pytest.raises(service.AuthError):
        await service.refresh_tokens(refresh_token=pair.refresh_token)


@pytest.mark.asyncio
async def test_concurrent_logins_do_not_exceed_limit(
    session_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一用户并发登录由用户行锁串行化，最终活跃 family 不超过上限。"""

    tenant_id, users, school = await _seed_tenant_users(("S-4111",))
    user_id = users["S-4111"]
    await _login(school, "S-4111")
    await _login(school, "S-4111")

    original_lock = sessions.acquire_user_lock
    both_ready = asyncio.Event()
    seen_tasks: set[int] = set()

    async def barrier_lock(
        session: AsyncSession, *, realm: sessions.Realm, user_id: UUID
    ) -> None:
        # 每次登录会获取两次锁（显式 + enforce_limit 重入）：只拦每个任务的首次获取，
        # 确保两个登录都到达后才有一个真正拿到事务锁。
        task_id = id(asyncio.current_task())
        if task_id not in seen_tasks:
            seen_tasks.add(task_id)
            if len(seen_tasks) == 2:
                both_ready.set()
            await asyncio.wait_for(both_ready.wait(), timeout=5)
        await original_lock(session, realm=realm, user_id=user_id)

    monkeypatch.setattr(sessions, "acquire_user_lock", barrier_lock)
    first, second = await asyncio.gather(
        _login(school, "S-4111", user_agent=CHROME_WINDOWS),
        _login(school, "S-4111", user_agent=SAFARI_IOS),
    )
    assert first.session_id != second.session_id
    assert await _active_family_count_tenant(tenant_id, user_id) == 3
    assert len(await _audit_rows("auth.session.kicked")) == 1


@pytest.mark.asyncio
async def test_revoke_wins_over_inflight_refresh(
    session_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """刷新先读后等锁时，撤销提交后刷新必须重读并失败，不得复活已下线会话。"""

    tenant_id, users, school = await _seed_tenant_users(("S-4113",))
    user_id = users["S-4113"]
    pair = await _login(school, "S-4113")

    original_lock = sessions.acquire_user_lock
    refresh_waiting = asyncio.Event()
    release_refresh = asyncio.Event()
    calls = 0

    async def delayed_lock(
        session: AsyncSession, *, realm: sessions.Realm, user_id: UUID
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            refresh_waiting.set()
            await release_refresh.wait()
        await original_lock(session, realm=realm, user_id=user_id)

    monkeypatch.setattr(sessions, "acquire_user_lock", delayed_lock)
    refresh_task = asyncio.create_task(
        service.refresh_tokens(refresh_token=pair.refresh_token)
    )
    await asyncio.wait_for(refresh_waiting.wait(), timeout=5)

    assert (
        await sessions.revoke_session(
            realm="tenant",
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=pair.session_id,
            current_session_id=pair.session_id,
            request_id="test-revoke-vs-refresh",
        )
        is True
    )
    release_refresh.set()
    with pytest.raises(service.AuthError) as excinfo:
        await refresh_task

    assert excinfo.value.code == "INVALID_REFRESH_TOKEN"
    assert await _active_family_count_tenant(tenant_id, user_id) == 0
    assert await revocation.session_exists(pair.session_id, realm="tenant") is False
    assert await _audit_rows("auth.refresh.reuse_detected") == []
    assert await _family_reason_tenant(tenant_id, pair.session_id) == "session_revoked"


@pytest.mark.asyncio
async def test_change_password_wins_over_inflight_refresh(
    session_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """改密先读后等锁时，刷新必须重读并失败，不能带着旧密码态续期。"""

    tenant_id, users, school = await _seed_tenant_users(("S-4114",))
    user_id = users["S-4114"]
    pair = await _login(school, "S-4114")

    original_lock = sessions.acquire_user_lock
    refresh_waiting = asyncio.Event()
    release_refresh = asyncio.Event()
    calls = 0

    async def delayed_lock(
        session: AsyncSession, *, realm: sessions.Realm, user_id: UUID
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            refresh_waiting.set()
            await release_refresh.wait()
        await original_lock(session, realm=realm, user_id=user_id)

    monkeypatch.setattr(sessions, "acquire_user_lock", delayed_lock)
    refresh_task = asyncio.create_task(
        service.refresh_tokens(refresh_token=pair.refresh_token)
    )
    await asyncio.wait_for(refresh_waiting.wait(), timeout=5)

    new_pair = await service.change_password(
        principal_user_id=user_id,
        realm="tenant",
        tenant_id=tenant_id,
        current_password=TENANT_PASSWORD,
        new_password=NEW_PASSWORD,
        user_agent=FIREFOX_MAC,
    )
    release_refresh.set()
    with pytest.raises(service.AuthError) as excinfo:
        await refresh_task

    assert excinfo.value.code == "INVALID_REFRESH_TOKEN"
    assert await _family_reason_tenant(tenant_id, pair.session_id) == "password_change"
    assert await revocation.session_exists(pair.session_id, realm="tenant") is False
    assert await revocation.session_exists(new_pair.session_id, realm="tenant") is True
    assert await _audit_rows("auth.refresh.reuse_detected") == []


@pytest.mark.asyncio
async def test_rotated_token_replay_after_kick_is_revoked_not_reuse(
    session_env: None,
) -> None:
    """设备被踢后重放该家族已轮换的旧令牌，应按已撤销拒绝而非误报 reuse。"""

    tenant_id, _users, school = await _seed_tenant_users(("S-4115",))
    first = await _login(school, "S-4115", user_agent=CHROME_WINDOWS)
    rotated = await service.refresh_tokens(refresh_token=first.refresh_token)
    await _login(school, "S-4115", user_agent=SAFARI_IOS)
    await _login(school, "S-4115", user_agent=FIREFOX_MAC)
    # 第一台刷新过但最久未用：第 4 台登录时被踢
    await _set_last_seen_tenant(tenant_id, first.session_id, timedelta(hours=2))
    await _login(school, "S-4115", user_agent=CHROME_WINDOWS)

    assert await _family_reason_tenant(tenant_id, first.session_id) == "session_kicked"
    for token in (first.refresh_token, rotated.refresh_token):
        with pytest.raises(service.AuthError) as excinfo:
            await service.refresh_tokens(refresh_token=token)
        assert excinfo.value.code == "INVALID_REFRESH_TOKEN"
    assert await _audit_rows("auth.refresh.reuse_detected") == []


@pytest.mark.asyncio
async def test_expired_family_replay_is_not_reuse(session_env: None) -> None:
    """家族自然过期后重放已轮换旧令牌：按已失效拒绝，不误报 reuse 也不写审计。"""

    tenant_id, users, school = await _seed_tenant_users(("S-4117",))
    first = await _login(school, "S-4117")
    await service.refresh_tokens(refresh_token=first.refresh_token)
    await _expire_family_tenant(tenant_id, first.session_id)

    with pytest.raises(service.AuthError) as excinfo:
        await service.refresh_tokens(refresh_token=first.refresh_token)
    assert excinfo.value.code == "INVALID_REFRESH_TOKEN"

    assert await _family_reason_tenant(tenant_id, first.session_id) is None
    assert await _active_family_count_tenant(tenant_id, users["S-4117"]) == 0
    assert await _audit_rows("auth.refresh.reuse_detected") == []


@pytest.mark.asyncio
async def test_expired_rotated_token_with_active_family_is_not_reuse(
    session_env: None,
) -> None:
    """家族仍活跃但旧令牌自身已过期：按已失效拒绝，不撤销家族、不误报 reuse。"""

    tenant_id, users, school = await _seed_tenant_users(("S-4122",))
    first = await _login(school, "S-4122")
    rotated = await service.refresh_tokens(refresh_token=first.refresh_token)
    await _expire_rotated_rows_tenant(tenant_id, first.session_id)

    with pytest.raises(service.AuthError) as excinfo:
        await service.refresh_tokens(refresh_token=first.refresh_token)
    assert excinfo.value.code == "INVALID_REFRESH_TOKEN"

    assert await _family_reason_tenant(tenant_id, first.session_id) is None
    assert await _active_family_count_tenant(tenant_id, users["S-4122"]) == 1
    assert await _audit_rows("auth.refresh.reuse_detected") == []
    still_valid = await service.refresh_tokens(refresh_token=rotated.refresh_token)
    assert still_valid.session_id == first.session_id


@pytest.mark.asyncio
async def test_reuse_detection_revokes_family_and_removes_session_marker(
    session_env: None,
) -> None:
    """真正的已轮换令牌重放：撤销家族、删除会话标记、旧访问令牌立即 401。"""

    tenant_id, users, school = await _seed_tenant_users(("S-4116",))
    user_id = users["S-4116"]
    first = await _login(school, "S-4116")
    rotated = await service.refresh_tokens(refresh_token=first.refresh_token)

    with pytest.raises(service.AuthError) as excinfo:
        await service.refresh_tokens(refresh_token=first.refresh_token)
    assert excinfo.value.code == "REFRESH_REUSE_DETECTED"

    assert await _family_reason_tenant(tenant_id, first.session_id) == "reuse_detected"
    assert await _active_family_count_tenant(tenant_id, user_id) == 0
    assert await revocation.session_exists(first.session_id, realm="tenant") is False
    await _expect_unauthorized(rotated)
    assert len(await _audit_rows("auth.refresh.reuse_detected")) == 1


@pytest.mark.asyncio
async def test_list_sessions_filters_family_without_redis_marker(
    session_env: None,
) -> None:
    """数据库仍活跃但会话标记缺失（如标记删除成功、DB 撤销未落）的设备不可见。"""

    tenant_id, users, school = await _seed_tenant_users(("S-4118",))
    pair = await _login(school, "S-4118")
    await revocation.delete_session(pair.session_id, realm="tenant")

    listed = await sessions.list_sessions(
        realm="tenant",
        tenant_id=tenant_id,
        user_id=users["S-4118"],
        current_session_id=pair.session_id,
    )
    assert listed == []
    assert await _active_family_count_tenant(tenant_id, users["S-4118"]) == 1


@pytest.mark.asyncio
async def test_kick_wins_over_inflight_refresh(
    session_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """刷新先读后等锁时被新登录踢出：必须重读并按 revoked 失败，不得误报 reuse。"""

    tenant_id, users, school = await _seed_tenant_users(("S-4119",))
    first = await _login(school, "S-4119")

    original_lock = sessions.acquire_user_lock
    refresh_waiting = asyncio.Event()
    release_refresh = asyncio.Event()
    calls = 0

    async def delayed_lock(
        session: AsyncSession, *, realm: sessions.Realm, user_id: UUID
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            refresh_waiting.set()
            await release_refresh.wait()
        await original_lock(session, realm=realm, user_id=user_id)

    monkeypatch.setattr(sessions, "acquire_user_lock", delayed_lock)
    refresh_task = asyncio.create_task(
        service.refresh_tokens(refresh_token=first.refresh_token)
    )
    await asyncio.wait_for(refresh_waiting.wait(), timeout=5)

    await _login(school, "S-4119", user_agent=SAFARI_IOS)
    await _login(school, "S-4119", user_agent=FIREFOX_MAC)
    await _login(school, "S-4119", user_agent=SAFARI_IOS)
    assert await _family_reason_tenant(tenant_id, first.session_id) == "session_kicked"

    release_refresh.set()
    with pytest.raises(service.AuthError) as excinfo:
        await refresh_task
    assert excinfo.value.code == "INVALID_REFRESH_TOKEN"
    assert await _audit_rows("auth.refresh.reuse_detected") == []
    assert await revocation.session_exists(first.session_id, realm="tenant") is False
    assert await _active_family_count_tenant(tenant_id, users["S-4119"]) == 3


@pytest.mark.asyncio
async def test_change_password_revoked_rotated_token_is_not_reuse(
    session_env: None,
) -> None:
    """改密撤销家族后重放其已轮换旧令牌：按 revoked 拒绝，不误报 reuse。"""

    tenant_id, users, school = await _seed_tenant_users(("S-4120",))
    first = await _login(school, "S-4120")
    rotated = await service.refresh_tokens(refresh_token=first.refresh_token)
    await service.change_password(
        principal_user_id=users["S-4120"],
        realm="tenant",
        tenant_id=tenant_id,
        current_password=TENANT_PASSWORD,
        new_password=NEW_PASSWORD,
    )

    for token in (first.refresh_token, rotated.refresh_token):
        with pytest.raises(service.AuthError) as excinfo:
            await service.refresh_tokens(refresh_token=token)
        assert excinfo.value.code == "INVALID_REFRESH_TOKEN"
    assert await _family_reason_tenant(tenant_id, first.session_id) == "password_change"
    assert await _audit_rows("auth.refresh.reuse_detected") == []


@pytest.mark.asyncio
async def test_lowered_limit_kicks_multiple_oldest_sessions(
    session_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上限从 3 下调到 1 后，下一次登录一次踢出全部超额设备。"""

    tenant_id, users, school = await _seed_tenant_users(("S-4121",))
    for user_agent in (CHROME_WINDOWS, SAFARI_IOS, FIREFOX_MAC):
        await _login(school, "S-4121", user_agent=user_agent)
    assert await _active_family_count_tenant(tenant_id, users["S-4121"]) == 3

    monkeypatch.setenv("MAX_SESSIONS_PER_USER_TENANT", "1")
    get_settings.cache_clear()
    await _login(school, "S-4121", user_agent=CHROME_WINDOWS)

    assert await _active_family_count_tenant(tenant_id, users["S-4121"]) == 1
    assert len(await _audit_rows("auth.session.kicked")) == 3
