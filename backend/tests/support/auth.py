"""认证测试支撑：平台账号种子与登录（M1-C）。

平台账号不开放注册，测试用管理连接直接写入 `platform_users/platform_roles/...`，
再通过 `/api/v1/auth/login` 走真实登录换取令牌（不绕过认证层）。
"""

from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.auth.passwords import hash_password

# 测试平台账号默认口令（≥10 位；不作为密钥使用）
TEST_PLATFORM_PASSWORD = "Platform-Test-Pass-1"


async def seed_platform_user(
    engine: AsyncEngine,
    *,
    account: str,
    password: str = TEST_PLATFORM_PASSWORD,
    permissions: tuple[str, ...] = ("platform.tenant.review", "platform.tenant.view"),
    must_change_password: bool = False,
    status: str = "active",
) -> str:
    """写入一个具备指定权限码的平台账号，返回账号名。

    参数:
        engine: 管理连接引擎（测试库超级用户）。
        account: 平台账号。
        password: 初始密码（测试内使用）。
        permissions: 期望授予的权限码。
        must_change_password: 是否处于首登改密状态。
        status: active/disabled。

    返回值:
        账号名。
    """

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        user_id = await session.scalar(
            text(
                "insert into platform_users "
                "(account, password_hash, status, must_change_password, mfa_enabled) "
                "values (:account, :hash, :status, :must_change, false) returning id"
            ),
            {
                "account": account,
                "hash": hash_password(password),
                "status": status,
                "must_change": must_change_password,
            },
        )
        role_code = f"platform_test_role_{uuid4().hex[:8]}"
        role_id = await session.scalar(
            text(
                "insert into platform_roles (code, name) values (:code, :name) returning id"
            ),
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
