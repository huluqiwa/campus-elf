"""M1-C 集成测试：登录、刷新轮换/重用、登出、改密与 realm↔连接绑定（真实 PG + Redis）。"""

import os
from collections.abc import AsyncIterator, Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.auth import principal as principal_module
from app.auth import revocation, service
from app.auth.passwords import hash_password
from app.auth.principal import get_current_principal
from app.auth.tokens import create_access_token
from app.config import Settings, get_settings
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


async def _seed_tenant_user(
    *, login_id: str, password: str, must_change_password: bool = False
) -> tuple[UUID, UUID]:
    """写入租户 + 用户，返回 (tenant_id, user_id)。"""

    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session, session.begin():
            tenant_id = await session.scalar(
                text(
                    "insert into tenants (school_code, name, status, activated_at) "
                    "values ('SCH-AUTHTEST1', '认证测试中学', 'active', now()) returning id"
                )
            )
            user_id = await session.scalar(
                text(
                    "insert into users (tenant_id, user_type, login_id, display_name, "
                    "password_hash, status, must_change_password, mfa_enabled) "
                    "values (:tenant_id, 'student', :login_id, '测试学生', :hash, 'active', "
                    ":must_change, false) returning id"
                ),
                {
                    "tenant_id": tenant_id,
                    "login_id": login_id,
                    "hash": hash_password(password),
                    "must_change": must_change_password,
                },
            )
        return tenant_id, user_id
    finally:
        await engine.dispose()


async def _audit_actions() -> list[str]:
    """读取全部审计动作（平台连接）。"""

    async with db_session(platform=True) as session:
        return list((await session.execute(text("select action from audit_logs"))).scalars().all())


@pytest.mark.asyncio
async def test_tenant_login_success_and_failure_audit(auth_env: None) -> None:
    """租户登录成功签发令牌并写审计；错误密码与未知学校返回同一文案。"""

    tenant_id, user_id = await _seed_tenant_user(login_id="S-1001", password=TENANT_PASSWORD)

    pair = as_token_pair(
        await service.login_tenant(
            school_code="SCH-AUTHTEST1", login_id="S-1001", password=TENANT_PASSWORD
        )
    )
    assert pair.realm == "tenant"
    assert pair.user_id == user_id
    assert pair.tenant_id == tenant_id
    assert pair.must_change_password is False

    async with db_session(tenant_id=tenant_id) as session:
        stored = (
            await session.execute(
                text("select jti, token_hash, family_id from refresh_tokens")
            )
        ).mappings().one()
    assert pair.refresh_token not in str(stored["token_hash"])

    with pytest.raises(service.AuthError) as wrong_password:
        await service.login_tenant(
            school_code="SCH-AUTHTEST1", login_id="S-1001", password="wrong-password"
        )
    with pytest.raises(service.AuthError) as unknown_school:
        await service.login_tenant(
            school_code="SCH-NOPE", login_id="S-1001", password=TENANT_PASSWORD
        )

    assert wrong_password.value.message == unknown_school.value.message
    assert wrong_password.value.http_status == 401

    actions = await _audit_actions()
    assert actions.count("auth.login.success") == 1
    assert actions.count("auth.login.failed") == 2


@pytest.mark.asyncio
async def test_refresh_rotation_and_reuse_detection(auth_env: None) -> None:
    """刷新轮换旧令牌标记 rotated；旧令牌再次使用 → 撤销整个家族并审计。"""

    tenant_id, _ = await _seed_tenant_user(login_id="S-1002", password=TENANT_PASSWORD)
    pair = as_token_pair(
        await service.login_tenant(
            school_code="SCH-AUTHTEST1", login_id="S-1002", password=TENANT_PASSWORD
        )
    )

    rotated = await service.refresh_tokens(refresh_token=pair.refresh_token)
    assert rotated.refresh_token != pair.refresh_token

    async with db_session(tenant_id=tenant_id) as session:
        rows = (
            await session.execute(
                text("select revoked_reason, rotated_to_jti from refresh_tokens order by issued_at")
            )
        ).mappings().all()
    assert rows[0]["revoked_reason"] == "rotated"
    assert rows[0]["rotated_to_jti"] is not None
    assert rows[1]["revoked_reason"] is None

    with pytest.raises(service.AuthError) as reuse:
        await service.refresh_tokens(refresh_token=pair.refresh_token)
    assert reuse.value.code == "REFRESH_REUSE_DETECTED"

    # 家族全部被撤销：最新令牌也不能再用
    with pytest.raises(service.AuthError):
        await service.refresh_tokens(refresh_token=rotated.refresh_token)

    async with db_session(tenant_id=tenant_id) as session:
        reasons: list[str] = list(
            (
                await session.execute(text("select revoked_reason from refresh_tokens"))
            ).scalars().all()
        )
    # 轮换的旧行保留 'rotated'，家族里未撤销的最新行被标记为 'reuse_detected'
    assert sorted(reasons) == ["reuse_detected", "rotated"]
    assert "auth.refresh.reuse_detected" in await _audit_actions()


@pytest.mark.asyncio
async def test_logout_revokes_family(auth_env: None) -> None:
    """登出撤销刷新令牌家族，之后刷新失败。"""

    _, user_id = await _seed_tenant_user(login_id="S-1003", password=TENANT_PASSWORD)
    pair = as_token_pair(
        await service.login_tenant(
            school_code="SCH-AUTHTEST1", login_id="S-1003", password=TENANT_PASSWORD
        )
    )

    await service.revoke_refresh_family(
        refresh_token=pair.refresh_token,
        actor_user_id=user_id,
        actor_realm="tenant",
    )

    with pytest.raises(service.AuthError):
        await service.refresh_tokens(refresh_token=pair.refresh_token)
    assert "auth.logout" in await _audit_actions()


@pytest.mark.asyncio
async def test_change_password_revokes_sessions_and_clears_flag(auth_env: None) -> None:
    """改密：旧密码校验、token_version 自增、全部家族撤销、must_change_password 清除。"""

    tenant_id, user_id = await _seed_tenant_user(
        login_id="S-1004", password=TENANT_PASSWORD, must_change_password=True
    )
    pair = as_token_pair(
        await service.login_tenant(
            school_code="SCH-AUTHTEST1", login_id="S-1004", password=TENANT_PASSWORD
        )
    )
    assert pair.must_change_password is True

    with pytest.raises(service.AuthError):
        await service.change_password(
            principal_user_id=user_id,
            realm="tenant",
            tenant_id=tenant_id,
            current_password="not-the-password",
            new_password="Brand-New-Pass-1",
        )

    new_pair = await service.change_password(
        principal_user_id=user_id,
        realm="tenant",
        tenant_id=tenant_id,
        current_password=TENANT_PASSWORD,
        new_password="Brand-New-Pass-1",
    )
    assert new_pair.must_change_password is False

    async with db_session(tenant_id=tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "select token_version, must_change_password, password_hash from users "
                    "where id = :uid"
                ),
                {"uid": user_id},
            )
        ).mappings().one()
        reasons: list[str] = list(
            (
                await session.execute(text("select revoked_reason from refresh_tokens"))
            ).scalars().all()
        )
    assert row["token_version"] == 1
    assert row["must_change_password"] is False
    assert "password_change" in reasons

    with pytest.raises(service.AuthError):
        await service.refresh_tokens(refresh_token=pair.refresh_token)


@pytest.mark.asyncio
async def test_platform_login_and_refresh_use_platform_table(auth_env: None) -> None:
    """平台登录写入 platform_refresh_tokens，且租户表无平台令牌。"""

    engine = make_test_engine("admin")
    try:
        await seed_platform_user(engine, account="ops-auth")
    finally:
        await engine.dispose()

    pair = as_token_pair(
        await service.login_platform(account="ops-auth", password=TEST_PLATFORM_PASSWORD)
    )
    assert pair.realm == "platform"
    assert pair.tenant_id is None

    refreshed = await service.refresh_tokens(refresh_token=pair.refresh_token)
    assert refreshed.realm == "platform"

    async with db_session(platform=True) as session:
        platform_rows = (
            await session.execute(text("select count(*) from platform_refresh_tokens"))
        ).scalar()
    assert platform_rows == 2


@pytest.mark.asyncio
async def test_principal_uses_realm_specific_connection(
    auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TD-074：平台主体只走平台连接，租户主体只走租户连接。"""

    from starlette.requests import Request

    tenant_id, user_id = await _seed_tenant_user(login_id="S-1005", password=TENANT_PASSWORD)
    engine = make_test_engine("admin")
    try:
        await seed_platform_user(engine, account="ops-binding")
    finally:
        await engine.dispose()

    settings = get_settings()
    captured: list[dict[str, object]] = []

    def _spy(**kwargs: object) -> object:
        captured.append(kwargs)
        return db_session(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(principal_module, "db_session", _spy)

    tenant_session = uuid4()
    platform_session = uuid4()
    await revocation.register_session(tenant_session, realm="tenant")
    await revocation.register_session(platform_session, realm="platform")
    tenant_token, _ = create_access_token(
        settings,
        user_id=user_id,
        realm="tenant",
        tenant_id=tenant_id,
        token_version=0,
        must_change_password=False,
        family_id=tenant_session,
    )
    platform_token, _ = create_access_token(
        settings,
        user_id=(await _platform_user_id()),
        realm="platform",
        tenant_id=None,
        token_version=0,
        must_change_password=False,
        family_id=platform_session,
    )

    def _request() -> Request:
        return Request(
            scope={
                "type": "http",
                "method": "GET",
                "path": "/api/v1/auth/me",
                "headers": [],
                "query_string": b"",
                "state": {},
            }
        )

    tenant_principal = await get_current_principal(_request(), f"Bearer {tenant_token}")
    assert tenant_principal.realm == "tenant"
    assert captured[-1] == {"tenant_id": tenant_id}

    platform_principal = await get_current_principal(_request(), f"Bearer {platform_token}")
    assert platform_principal.realm == "platform"
    assert captured[-1] == {"platform": True}


async def _platform_user_id() -> UUID:
    """读取刚才种子的平台账号 id。"""

    async with db_session(platform=True) as session:
        value = await session.scalar(
            text("select id from platform_users where account = 'ops-binding'")
        )
    assert value is not None
    return UUID(str(value))


@pytest.mark.asyncio
async def test_operator_settings_are_isolated() -> None:
    """配置隔离：测试内构造的 Settings 不影响进程缓存（防跨用例串配置）。"""

    settings = Settings(
        _env_file=None,
        redis_url="redis://127.0.0.1:6379/15",
        jwt_secret="another-jwt-secret-0123456789abcdefgh",
    )
    assert settings.jwt_issuer == "campus-elf"
    assert settings.access_token_ttl_seconds == 900


@pytest.mark.asyncio
async def test_cleanup_refresh_tokens_respects_retention(auth_env: None) -> None:
    """TD-067：清理过期/久已撤销的刷新令牌，保留期内的行不清理。"""

    from app.workers.tasks import cleanup_refresh_tokens

    tenant_id, user_id = await _seed_tenant_user(login_id="S-1006", password=TENANT_PASSWORD)

    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session, session.begin():
            base = dict(tenant_id=tenant_id, user_id=user_id, family_id=uuid4())
            await session.execute(
                text(
                    "insert into refresh_tokens "
                    "(tenant_id, user_id, jti, family_id, token_hash, expires_at, "
                    " last_seen_at, revoked_at, revoked_reason) "
                    "values (:tenant_id, :user_id, :jti, :family_id, :hash, "
                    " now() - interval '40 days', now() - interval '40 days', null, null)"
                ),
                {**base, "jti": uuid4(), "hash": "a" * 64},
            )
            await session.execute(
                text(
                    "insert into refresh_tokens "
                    "(tenant_id, user_id, jti, family_id, token_hash, expires_at, "
                    " last_seen_at, revoked_at, revoked_reason) "
                    "values (:tenant_id, :user_id, :jti, :family_id, :hash, "
                    " now() + interval '1 day', now() - interval '1 day', "
                    " now() - interval '40 days', 'logout')"
                ),
                {**base, "jti": uuid4(), "hash": "b" * 64},
            )
            await session.execute(
                text(
                    "insert into refresh_tokens "
                    "(tenant_id, user_id, jti, family_id, token_hash, expires_at, "
                    " last_seen_at, revoked_at, revoked_reason) "
                    "values (:tenant_id, :user_id, :jti, :family_id, :hash, "
                    " now() + interval '1 day', now() - interval '1 day', "
                    " now() - interval '1 day', 'logout')"
                ),
                {**base, "jti": uuid4(), "hash": "c" * 64},
            )
            await session.execute(
                text(
                    "insert into refresh_tokens "
                    "(tenant_id, user_id, jti, family_id, token_hash, expires_at, "
                    " last_seen_at) "
                    "values (:tenant_id, :user_id, :jti, :family_id, :hash, "
                    " now() + interval '7 days', now())"
                ),
                {**base, "jti": uuid4(), "hash": "d" * 64},
            )
    finally:
        await engine.dispose()

    result = await cleanup_refresh_tokens({})
    assert result["tenant_tokens"] == 2

    async with db_session(tenant_id=tenant_id) as session:
        remaining: list[str] = list(
            (
                await session.execute(text("select token_hash from refresh_tokens order by token_hash"))
            ).scalars().all()
        )
    assert remaining == ["c" * 64, "d" * 64]


@pytest.mark.asyncio
async def test_unknown_tenant_refresh_returns_401_not_500(auth_env: None) -> None:
    """未知 tenant_id 的刷新令牌：401 且审计降级为平台事件（不因外键违约 500）。"""

    forged = f"v1t.{uuid4()}.{uuid4()}.{'x' * 32}"
    with pytest.raises(service.AuthError) as excinfo:
        await service.refresh_tokens(refresh_token=forged)
    assert excinfo.value.code == "INVALID_REFRESH_TOKEN"

    rolls_back = await _audit_actions()
    assert "auth.refresh.failed" in rolls_back


@pytest.mark.asyncio
async def test_logout_requires_valid_refresh_secret(auth_env: None) -> None:
    """登出必须校验刷新令牌 secret：伪造 secret 不能撤销他人家族，审计也不记他人。"""

    tenant_id, user_id = await _seed_tenant_user(login_id="S-1007", password=TENANT_PASSWORD)
    pair = as_token_pair(
        await service.login_tenant(
            school_code="SCH-AUTHTEST1", login_id="S-1007", password=TENANT_PASSWORD
        )
    )
    _, _, jti = service.parse_refresh_token(pair.refresh_token)

    forged = f"v1t.{tenant_id}.{jti}.{'y' * 32}"
    revoked = await service.revoke_refresh_family(
        refresh_token=forged, actor_user_id=uuid4(), actor_realm="tenant"
    )
    assert revoked is None

    # 真实刷新令牌仍然可用（未被伪造请求撤销）
    refreshed = await service.refresh_tokens(refresh_token=pair.refresh_token)
    assert refreshed.user_id == user_id

    async with db_session(tenant_id=tenant_id) as session:
        reasons: list[str | None] = list(
            (
                await session.execute(text("select revoked_reason from refresh_tokens"))
            ).scalars().all()
        )
    assert "logout" not in reasons


@pytest.mark.asyncio
async def test_cleanup_refresh_tokens_counts_platform_rows(auth_env: None) -> None:
    """清理任务同时清理平台表并返回真实条数（审查 Minor 1）。"""

    from app.workers.tasks import cleanup_refresh_tokens

    engine = make_test_engine("admin")
    try:
        await seed_platform_user(engine, account="ops-cleanup")
    finally:
        await engine.dispose()
    await service.login_platform(account="ops-cleanup", password=TEST_PLATFORM_PASSWORD)

    async with db_session(platform=True) as session:
        await session.execute(
            text(
                "update platform_refresh_tokens set expires_at = now() - interval '40 days'"
            )
        )

    result = await cleanup_refresh_tokens({})
    assert result["platform_tokens"] == 1


@pytest.mark.asyncio
async def test_db_guard_rejects_platform_permission_for_tenant_role(auth_env: None) -> None:
    """数据库守卫：租户角色引用 platform.* 权限直接被拒（B1 第二道防线）。"""

    from sqlalchemy.exc import DBAPIError

    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session, session.begin():
            tenant_id = await session.scalar(
                text(
                    "insert into tenants (school_code, name, status, activated_at) "
                    "values ('SCH-GUARD1', '矩阵守卫中学', 'active', now()) returning id"
                )
            )
            role_id = await session.scalar(
                text(
                    "insert into roles (tenant_id, code, name, is_system) "
                    "values (:tid, 'guard_role', '守卫角色', false) returning id"
                ),
                {"tid": tenant_id},
            )
            permission_id = await session.scalar(
                text("select id from permissions where code = 'platform.tenant.view'")
            )
        with pytest.raises(DBAPIError) as excinfo:
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "insert into role_permissions (tenant_id, role_id, permission_id) "
                        "values (:tid, :rid, :pid)"
                    ),
                    {"tid": tenant_id, "rid": role_id, "pid": permission_id},
                )
        assert "cannot reference platform permissions" in str(excinfo.value)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_selfcheck_detects_tenant_role_with_platform_permission(auth_env: None) -> None:
    """M1 复审：矩阵自检必须能发现"租户角色持有 platform.*"（不能因 RLS 恒为 0）。"""

    from app.db.selfcheck import DatabaseSelfCheckError, verify_runtime_database

    tenant_id, _ = await _seed_tenant_user(login_id="S-1008", password=TENANT_PASSWORD)

    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session, session.begin():
            await session.execute(
                text("alter table role_permissions disable trigger role_permissions_realm_guard")
            )
            try:
                role_id = await session.scalar(
                    text(
                        "insert into roles (tenant_id, code, name, is_system) "
                        "values (:tid, 'violation_role', '越界角色', false) returning id"
                    ),
                    {"tid": tenant_id},
                )
                await session.execute(
                    text(
                        "insert into role_permissions (tenant_id, role_id, permission_id) "
                        "select :tid, :rid, id from permissions "
                        "where code = 'platform.tenant.view'"
                    ),
                    {"tid": tenant_id, "rid": role_id},
                )
            finally:
                await session.execute(
                    text("alter table role_permissions enable trigger role_permissions_realm_guard")
                )

        with pytest.raises(DatabaseSelfCheckError, match="platform"):
            await verify_runtime_database()
    finally:
        # 清理植入数据，避免与其他用例相互影响
        try:
            factory = make_session_factory(engine)
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "delete from role_permissions where role_id in "
                        "(select id from roles where code = 'violation_role')"
                    )
                )
                await session.execute(text("delete from roles where code = 'violation_role'"))
        finally:
            await engine.dispose()
