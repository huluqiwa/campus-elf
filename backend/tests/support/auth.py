"""认证测试支撑：平台账号种子与登录（M1-C）。

平台账号不开放注册，测试用管理连接直接写入 `platform_users/platform_roles/...`，
再通过 `/api/v1/auth/login` 走真实登录换取令牌（不绕过认证层）。
"""

from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.auth import service
from app.auth.passwords import hash_password

# 测试平台账号默认口令（≥10 位；不作为密钥使用）
TEST_PLATFORM_PASSWORD = "Platform-Test-Pass-1"


def as_token_pair(outcome: service.LoginOutcome) -> service.TokenPair:
    """收窄登录结果：断言该账号未启用 MFA，直接拿到了会话令牌。"""

    assert isinstance(outcome, service.TokenPair), f"期望直接签发会话，实际得到 {outcome}"
    return outcome


async def seed_platform_user(
    engine: AsyncEngine,
    *,
    account: str,
    password: str = TEST_PLATFORM_PASSWORD,
    permissions: tuple[str, ...] = ("platform.tenant.review", "platform.tenant.view"),
    must_change_password: bool = False,
    status: str = "active",
    email: str | None = None,
    mfa_enabled: bool = False,
    mfa_secret_enc: str | None = None,
    mfa_last_used_step: int | None = None,
) -> str:
    """写入一个具备指定权限码的平台账号，返回账号名。

    参数:
        engine: 管理连接引擎（测试库超级用户）。
        account: 平台账号。
        password: 初始密码（测试内使用）。
        permissions: 期望授予的权限码。
        must_change_password: 是否处于首登改密状态。
        status: active/disabled。
        email: 密码重置收件地址（None 表示未登记）。
        mfa_enabled: 是否已启用 MFA。
        mfa_secret_enc: 加密后的 TOTP secret（由测试用 KEK 加密）。
        mfa_last_used_step: TOTP 重放水位。

    返回值:
        账号名。
    """

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        user_id = await session.scalar(
            text(
                "insert into platform_users "
                "(account, email, password_hash, status, must_change_password, mfa_enabled, "
                "mfa_secret_enc, mfa_last_used_step) "
                "values (:account, :email, :hash, :status, :must_change, :mfa_enabled, "
                ":mfa_secret_enc, :mfa_last_used_step) returning id"
            ),
            {
                "account": account,
                "email": email,
                "hash": hash_password(password),
                "status": status,
                "must_change": must_change_password,
                "mfa_enabled": mfa_enabled,
                "mfa_secret_enc": mfa_secret_enc,
                "mfa_last_used_step": mfa_last_used_step,
            },
        )
        role_code = f"platform_test_role_{uuid4().hex[:8]}"
        role_id = await session.scalar(
            text("insert into platform_roles (code, name) values (:code, :name) returning id"),
            {"code": role_code, "name": role_code},
        )
        if permissions:
            await session.execute(
                text(
                    "insert into platform_role_permissions (platform_role_id, permission_id) "
                    "select :role_id, p.id from permissions p where p.code = any(:codes)"
                ),
                {"role_id": role_id, "codes": list(permissions)},
            )
        await session.execute(
            text(
                "insert into platform_user_roles (platform_user_id, platform_role_id) "
                "values (:user_id, :role_id)"
            ),
            {"user_id": user_id, "role_id": role_id},
        )
    return account


async def seed_tenant_user(
    engine: AsyncEngine,
    *,
    school_code: str,
    login_id: str,
    password: str,
    user_type: str = "school_admin",
    email: str | None = None,
    must_change_password: bool = False,
    status: str = "active",
    mfa_enabled: bool = False,
    mfa_secret_enc: str | None = None,
    mfa_last_used_step: int | None = None,
) -> tuple[UUID, UUID]:
    """写入租户与用户（复用已有同 school_code 的租户），返回 (tenant_id, user_id)。"""

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        tenant_id = await session.scalar(
            text("select id from tenants where school_code = :code"), {"code": school_code}
        )
        if tenant_id is None:
            tenant_id = await session.scalar(
                text(
                    "insert into tenants (school_code, name, status, activated_at) "
                    "values (:code, '认证测试中学', 'active', now()) returning id"
                ),
                {"code": school_code},
            )
        user_id = await session.scalar(
            text(
                "insert into users (tenant_id, user_type, login_id, display_name, email, "
                "password_hash, status, must_change_password, mfa_enabled, "
                "mfa_secret_enc, mfa_last_used_step) "
                "values (:tenant_id, :user_type, :login_id, '测试用户', :email, :hash, "
                ":status, :must_change, :mfa_enabled, :mfa_secret_enc, :mfa_last_used_step) "
                "returning id"
            ),
            {
                "tenant_id": tenant_id,
                "user_type": user_type,
                "login_id": login_id,
                "email": email,
                "hash": hash_password(password),
                "status": status,
                "must_change": must_change_password,
                "mfa_enabled": mfa_enabled,
                "mfa_secret_enc": mfa_secret_enc,
                "mfa_last_used_step": mfa_last_used_step,
            },
        )
        return tenant_id, user_id


async def login_platform(
    client: AsyncClient,
    account: str,
    password: str = TEST_PLATFORM_PASSWORD,
) -> dict[str, str]:
    """平台账号登录，返回 Authorization 头。"""

    response = await client.post(
        "/api/v1/auth/login",
        json={"realm": "platform", "account": account, "password": password},
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
