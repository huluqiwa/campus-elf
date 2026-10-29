"""M1-D2 MFA 测试支撑：账号种子、TOTP 码生成与审计/令牌读取（集成与业务场景共用）。

这些用例都跑真实 PG + Redis，因此统一在此处提供"写入 MFA 状态"与"读取业务副作用"
的最小助手，避免每个测试文件各写一份 SQL。
"""

from uuid import UUID

import pyotp
from sqlalchemy import text

from app.auth import service
from app.config import get_settings
from app.db.session import db_session
from app.security.encryption import (
    decode_kek,
    encrypt_secret,
    generate_recovery_codes,
    mfa_aad,
    recovery_code_subkey,
)
from app.security.totp import generate_totp_secret
from tests.support.auth import seed_tenant_user
from tests.support.db import make_session_factory, make_test_engine

FIXED_STEP = 2_000_000
SCHOOL_CODE = "SCH-MFATEST"
TENANT_PASSWORD = "Tenant-Test-Pass-1"
# 长度合法但永远算不出 TOTP 的码（用于构造校验失败分支）
INVALID_CODE = "ZZZZZ-ZZZZZ"


def mfa_code(secret: str, step: int = FIXED_STEP) -> str:
    """生成指定时间步的 6 位 TOTP。"""

    interval = get_settings().mfa_totp_interval_seconds
    return pyotp.TOTP(secret, interval=interval).at(step * interval)


async def seed_tenant(login_id: str, *, school_code: str = SCHOOL_CODE) -> tuple[UUID, UUID]:
    """写入租户用户（MFA 未启用），返回 (tenant_id, user_id)。"""

    engine = make_test_engine("admin")
    try:
        return await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=login_id,
            password=TENANT_PASSWORD,
        )
    finally:
        await engine.dispose()


async def enable_tenant_mfa(
    tenant_id: UUID,
    user_id: UUID,
    secret: str,
    *,
    last_step: int | None = None,
    aad_user_id: UUID | None = None,
) -> None:
    """加密 secret 并启用 MFA（`aad_user_id` 用于构造"密文被移植"的故障场景）。"""

    encrypted = encrypt_secret(
        secret,
        kek=decode_kek(get_settings().encryption_kek),
        aad=mfa_aad("tenant", aad_user_id or user_id),
    )
    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "update users set mfa_enabled = true, mfa_secret_enc = :enc, "
                    "mfa_last_used_step = :step where tenant_id = :tid and id = :uid"
                ),
                {"enc": encrypted, "step": last_step, "tid": tenant_id, "uid": user_id},
            )
    finally:
        await engine.dispose()


async def enable_platform_mfa(user_id: UUID, secret: str) -> None:
    """为平台账号写入加密 secret 并启用 MFA。"""

    encrypted = encrypt_secret(
        secret, kek=decode_kek(get_settings().encryption_kek), aad=mfa_aad("platform", user_id)
    )
    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "update platform_users set mfa_enabled = true, mfa_secret_enc = :enc "
                    "where id = :uid"
                ),
                {"enc": encrypted, "uid": user_id},
            )
    finally:
        await engine.dispose()


async def platform_user_id(account: str) -> UUID:
    """读取平台账号 id。"""

    async with db_session(platform=True) as session:
        value = await session.scalar(
            text("select id from platform_users where account = :account"), {"account": account}
        )
    assert value is not None
    return UUID(str(value))


async def insert_recovery_codes(tenant_id: UUID, user_id: UUID, count: int = 2) -> list[str]:
    """插入恢复码摘要，返回明文码（仅测试内使用）。"""

    subkey = recovery_code_subkey(decode_kek(get_settings().encryption_kek))
    codes, hashes = generate_recovery_codes(count, subkey=subkey, realm="tenant", user_id=user_id)
    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session, session.begin():
            for digest in hashes:
                await session.execute(
                    text(
                        "insert into mfa_recovery_codes (tenant_id, user_id, code_hash) "
                        "values (:tid, :uid, :hash)"
                    ),
                    {"tid": tenant_id, "uid": user_id, "hash": digest},
                )
    finally:
        await engine.dispose()
    return codes


async def tenant_mfa_user(
    login_id: str, *, school_code: str = SCHOOL_CODE
) -> tuple[UUID, UUID, str]:
    """创建已启用 MFA 的租户用户，返回 (tenant_id, user_id, secret)。"""

    tenant_id, user_id = await seed_tenant(login_id, school_code=school_code)
    secret = generate_totp_secret()
    await enable_tenant_mfa(tenant_id, user_id, secret)
    return tenant_id, user_id, secret


async def tenant_login(login_id: str, *, school_code: str = SCHOOL_CODE) -> service.MfaChallenge:
    """密码登录并断言进入 MFA 第二步。"""

    outcome = await service.login_tenant(
        school_code=school_code, login_id=login_id, password=TENANT_PASSWORD
    )
    assert isinstance(outcome, service.MfaChallenge)
    return outcome


async def audit_metadata(
    action: str, tenant_id: UUID | None = None
) -> list[dict[str, object]]:
    """读取指定动作的审计 metadata（平台连接）。

    给了 `tenant_id` 就只取该租户的行（TD-228：精确条数断言不该被别租同名动作污染）；
    不给就是全表口径——"这条动作一次都不该发生"的断言要的是全表口径，按租户收窄会漏掉
    **写错租户**的那一行。
    """

    if tenant_id is None:
        statement = text(
            "select metadata_json from audit_logs where action = :action order by created_at"
        )
        params: dict[str, object] = {"action": action}
    else:
        statement = text(
            "select metadata_json from audit_logs where action = :action "
            "and tenant_id = :tid order by created_at"
        )
        params = {"action": action, "tid": str(tenant_id)}
    async with db_session(platform=True) as session:
        rows: list[dict[str, object] | None] = list(
            (await session.execute(statement, params)).scalars().all()
        )
    return [dict(row or {}) for row in rows]


async def refresh_rows(tenant_id: UUID) -> list[dict[str, object]]:
    """读取租户刷新令牌行。"""

    async with db_session(tenant_id=tenant_id) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select mfa_verified, revoked_reason from refresh_tokens "
                        "order by issued_at, id"
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def platform_refresh_rows() -> list[dict[str, object]]:
    """读取平台刷新令牌行。"""

    async with db_session(platform=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select mfa_verified, revoked_reason from platform_refresh_tokens "
                        "order by issued_at, id"
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def tenant_mfa_state(tenant_id: UUID, user_id: UUID) -> dict[str, object]:
    """读取租户用户的 MFA/锁定/版本状态。"""

    async with db_session(tenant_id=tenant_id) as session:
        row = (
            (
                await session.execute(
                    text(
                        "select mfa_enabled, mfa_secret_enc, mfa_last_used_step, "
                        "failed_login_attempts, locked_until, token_version "
                        "from users where tenant_id = :tid and id = :uid"
                    ),
                    {"tid": tenant_id, "uid": user_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def platform_mfa_state(user_id: UUID) -> dict[str, object]:
    """读取平台账号的 MFA/锁定/版本状态。"""

    async with db_session(platform=True) as session:
        row = (
            (
                await session.execute(
                    text(
                        "select mfa_enabled, mfa_secret_enc, mfa_last_used_step, "
                        "failed_login_attempts, locked_until, token_version "
                        "from platform_users where id = :uid"
                    ),
                    {"uid": user_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)
