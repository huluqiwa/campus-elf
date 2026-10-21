"""认证服务：三端登录、刷新轮换、登出、首登改密。

安全要点：
- 登录失败一律返回同一错误（不区分"学校编号不存在/账号不存在/密码错误"），失败也写审计；
- 刷新令牌只存 SHA-256 哈希；令牌串形如 `v1t.<tenant_id>.<jti>.<secret>` / `v1p.<jti>.<secret>`，
  便于按 realm 选择连接并在 RLS 下定位记录（secret 仍是唯一凭证）；
- 轮换用**条件更新**（`where revoked_at is null`）保证并发下只有一个成功；检测到已轮换令牌
  再次使用 → 撤销整个家族（`reuse_detected`）并写审计；
- 改密后 `token_version+1`（旧访问令牌立即失效）并撤销该用户全部刷新令牌家族；
- 失败路径一律**先退出事务**再单独落库（审计/家族撤销在独立事务提交），避免随回滚丢失；
- 所有 SQL 都是按 realm 写死的字面量（见 `_SQL`），不拼接表名，条件值走绑定参数。
"""

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import anyio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import append_audit
from app.auth import revocation
from app.auth import sessions as session_store
from app.auth.passwords import hash_password, verify_password
from app.auth.tokens import (
    Realm,
    create_access_token,
    generate_refresh_token,
    hash_client_fingerprint,
    hash_refresh_token,
)
from app.config import Settings, get_settings
from app.db.session import db_session

# 新密码最小长度（完整策略在 M1-D：历史/复杂度/与学号工号比对）
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128
# bcrypt 输入上限 72 字节（多字节按 UTF-8 计）：超长会抛 ValueError（500）。
# 入口按字节拦截；改哈希方案（argon2/预哈希）见 TD-107。
MAX_PASSWORD_BYTES = 72


def _password_within_bcrypt_limit(password: str) -> bool:
    """口令是否在 bcrypt 可处理范围内（UTF-8 字节数）。"""

    return len(password.encode("utf-8")) <= MAX_PASSWORD_BYTES

# 刷新令牌串的两类前缀（用于区分 realm；secret 才是凭证）
_TENANT_REFRESH_KIND = "v1t"
_PLATFORM_REFRESH_KIND = "v1p"

INVALID_CREDENTIALS_MESSAGE = "学校编号、账号或密码不正确"

# 账号不存在时用于"等时假比对"的占位摘要（bcrypt cost 12，与真实校验同耗时）：
# 登录失败路径统一付出一次 bcrypt 开销，避免用响应时间枚举账号是否存在（审查 Minor 6）。
# 该值不是任何账号的口令，仅用于消耗等价计算量。
_TIMING_EQUALIZATION_HASH = (
    "$2b$12$lmaZFg4Ynv/J4PEzLkCYlOkUhUL6H2ffvrAmrTHkoc14Q8Q.PuDEm"
)

# 按 realm 的字面量 SQL：表名不参与字符串拼接，便于审查"租户/平台各自访问哪些表"。
_SQL: dict[Realm, dict[str, str]] = {
    "tenant": {
        "revoke_family": (
            "update refresh_tokens set revoked_at = now(), revoked_reason = :reason "
            "where tenant_id = :tid and family_id = :family_id and user_id = :uid "
            "  and revoked_at is null returning family_id"
        ),
        "select_refresh": (
            "select id, user_id as user_id, family_id, token_hash, expires_at, revoked_at, "
            "revoked_reason, rotated_to_jti, device_label from refresh_tokens "
            "where tenant_id = :tid and jti = :jti"
        ),
        "family_revoked": (
            "select 1 from refresh_tokens "
            "where tenant_id = :tid and user_id = :uid and family_id = :fid "
            "  and revoked_at is not null "
            "  and revoked_reason is distinct from 'rotated' limit 1"
        ),
        "select_user_state": (
            "select status, token_version, must_change_password from users "
            "where tenant_id = :tid and id = :uid"
        ),
        "rotate_refresh": (
            "update refresh_tokens set rotated_to_jti = :new_jti, revoked_at = now(), "
            "revoked_reason = 'rotated' "
            "where tenant_id = :tid and id = :id and revoked_at is null returning id"
        ),
        "family_by_jti": (
            "select family_id, user_id, token_hash from refresh_tokens "
            "where tenant_id = :tid and jti = :jti"
        ),
        "select_credentials": (
            "select status, password_hash from users where tenant_id = :tid and id = :uid"
        ),
        "set_credential_hash": (
            "update users set password_hash = :hash, must_change_password = false, "
            "password_changed_at = now(), token_version = token_version + 1, "
            "updated_at = now() where tenant_id = :tid and id = :uid"
        ),
        "revoke_user_tokens": (
            "update refresh_tokens set revoked_at = now(), revoked_reason = 'password_change' "
            "where tenant_id = :tid and user_id = :uid and revoked_at is null"
        ),
        "select_credential_version": (
            "select token_version from users where tenant_id = :tid and id = :uid"
        ),
        "active_families": (
            "select distinct family_id from refresh_tokens "
            "where tenant_id = :tid and user_id = :uid and revoked_at is null"
        ),
    },
    "platform": {
        "revoke_family": (
            "update platform_refresh_tokens set revoked_at = now(), revoked_reason = :reason, "
            "updated_at = now() where family_id = :family_id and platform_user_id = :uid "
            "  and revoked_at is null returning family_id"
        ),
        "select_refresh": (
            "select id, platform_user_id as user_id, family_id, token_hash, expires_at, "
            "revoked_at, revoked_reason, rotated_to_jti, device_label "
            "from platform_refresh_tokens where jti = :jti"
        ),
        "family_revoked": (
            "select 1 from platform_refresh_tokens "
            "where platform_user_id = :uid and family_id = :fid and revoked_at is not null "
            "  and revoked_reason is distinct from 'rotated' limit 1"
        ),
        "select_user_state": (
            "select status, token_version, must_change_password from platform_users "
            "where id = :uid"
        ),
        "rotate_refresh": (
            "update platform_refresh_tokens set rotated_to_jti = :new_jti, revoked_at = now(), "
            "revoked_reason = 'rotated', updated_at = now() "
            "where id = :id and revoked_at is null returning id"
        ),
        "family_by_jti": (
            "select family_id, platform_user_id as user_id, token_hash "
            "from platform_refresh_tokens where jti = :jti"
        ),
        "select_credentials": "select status, password_hash from platform_users where id = :uid",
        "set_credential_hash": (
            "update platform_users set password_hash = :hash, must_change_password = false, "
            "password_changed_at = now(), token_version = token_version + 1, "
            "updated_at = now() where id = :uid"
        ),
        "revoke_user_tokens": (
            "update platform_refresh_tokens set revoked_at = now(), "
            "revoked_reason = 'password_change', updated_at = now() "
            "where platform_user_id = :uid and revoked_at is null"
        ),
        "select_credential_version": "select token_version from platform_users where id = :uid",
        "active_families": (
            "select distinct family_id from platform_refresh_tokens "
            "where platform_user_id = :uid and revoked_at is null"
        ),
    },
}


def _refresh_insert_sql(realm: Realm) -> str:
    """刷新令牌插入语句（租户/平台各一条字面量）。"""

    if realm == "tenant":
        return (
            "insert into refresh_tokens "
            "(id, tenant_id, user_id, jti, family_id, token_hash, expires_at, "
            "last_seen_at, device_label, ua_hash, ip_hash) "
            "values (gen_random_uuid(), :tenant_id, :user_id, :jti, :family_id, "
            ":token_hash, :expires_at, :last_seen_at, :device_label, :ua_hash, :ip_hash)"
        )
    return (
        "insert into platform_refresh_tokens "
        "(id, platform_user_id, jti, family_id, token_hash, expires_at, "
        "last_seen_at, device_label, ua_hash, ip_hash) "
        "values (gen_random_uuid(), :user_id, :jti, :family_id, "
        ":token_hash, :expires_at, :last_seen_at, :device_label, :ua_hash, :ip_hash)"
    )


@dataclass(frozen=True, slots=True)
class TokenPair:
    """登录/刷新结果（明文令牌只在此处出现，不写日志/审计）。"""

    access_token: str
    refresh_token: str
    expires_in: int
    realm: Realm
    user_id: UUID
    tenant_id: UUID | None
    session_id: UUID
    must_change_password: bool
    roles: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class FamilyRevocation:
    """刷新令牌家族的撤销结果（含归属，供跨租/跨用户登出写正确审计）。"""

    family_id: UUID
    revoked: bool
    realm: Realm
    tenant_id: UUID | None
    subject_user_id: UUID


class AuthError(Exception):
    """认证业务错误（由接口层映射为 HTTP 状态码）。"""

    def __init__(self, code: str, message: str, http_status: int) -> None:
        """初始化错误。"""

        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class _RefreshFailure(Exception):
    """内部信号：刷新/改密业务失败（携带原因与家族信息，供外层在独立事务落库）。"""

    def __init__(
        self,
        reason: str,
        *,
        family_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> None:
        """初始化失败信号。"""

        super().__init__(reason)
        self.reason = reason
        self.family_id = family_id
        self.user_id = user_id


def _invalid_credentials() -> AuthError:
    """统一的登录失败错误（不泄露账号是否存在）。"""

    return AuthError("INVALID_CREDENTIALS", INVALID_CREDENTIALS_MESSAGE, 401)


def _build_refresh_token(realm: Realm, jti: UUID, tenant_id: UUID | None) -> str:
    """构造刷新令牌串（含定位信息，secret 为唯一凭证）。"""

    secret = generate_refresh_token()
    if realm == "tenant":
        if tenant_id is None:
            raise ValueError("租户刷新令牌必须有 tenant_id")
        return f"{_TENANT_REFRESH_KIND}.{tenant_id}.{jti}.{secret}"
    return f"{_PLATFORM_REFRESH_KIND}.{jti}.{secret}"


def parse_refresh_token(token: str) -> tuple[Realm, UUID | None, UUID]:
    """解析刷新令牌的 realm/tenant_id/jti（不校验 secret）。

    异常:
        AuthError: 格式非法。
    """

    parts = token.strip().split(".")
    if len(parts) == 4 and parts[0] == _TENANT_REFRESH_KIND:
        try:
            return "tenant", UUID(parts[1]), UUID(parts[2])
        except ValueError as exc:
            raise AuthError("INVALID_REFRESH_TOKEN", "刷新令牌无效", 401) from exc
    if len(parts) == 3 and parts[0] == _PLATFORM_REFRESH_KIND:
        try:
            return "platform", None, UUID(parts[1])
        except ValueError as exc:
            raise AuthError("INVALID_REFRESH_TOKEN", "刷新令牌无效", 401) from exc
    raise AuthError("INVALID_REFRESH_TOKEN", "刷新令牌无效", 401)


async def _issue_tokens(
    settings: Settings,
    *,
    realm: Realm,
    user_id: UUID,
    tenant_id: UUID | None,
    token_version: int,
    must_change_password: bool,
    ip_hash: str | None,
    ua_hash: str | None,
    session: AsyncSession,
    family_id: UUID | None = None,
    device_label: str | None = None,
    roles: frozenset[str] = frozenset(),
) -> TokenPair:
    """签发访问令牌并写入新的刷新令牌行（同一事务）。"""

    jti = uuid4()
    family = family_id or uuid4()
    refresh_token = _build_refresh_token(realm, jti, tenant_id)
    access_token, _ = create_access_token(
        settings,
        user_id=user_id,
        realm=realm,
        tenant_id=tenant_id,
        family_id=family,
        token_version=token_version,
        must_change_password=must_change_password,
    )
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.refresh_token_ttl_seconds)
    params: dict[str, object] = {
        "user_id": user_id,
        "jti": jti,
        "family_id": family,
        "token_hash": hash_refresh_token(refresh_token),
        "expires_at": expires_at,
        "last_seen_at": now,
        "device_label": device_label,
        "ua_hash": ua_hash,
        "ip_hash": ip_hash,
    }
    if realm == "tenant":
        params["tenant_id"] = tenant_id
    await session.execute(text(_refresh_insert_sql(realm)), params)
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_ttl_seconds,
        realm=realm,
        user_id=user_id,
        tenant_id=tenant_id,
        session_id=family,
        must_change_password=must_change_password,
        roles=roles,
    )


async def _load_roles(
    session: AsyncSession, *, realm: Realm, tenant_id: UUID | None, user_id: UUID
) -> frozenset[str]:
    """读取主体当前角色码（登录/刷新/改密响应与审计使用）。"""

    params: dict[str, object] = {"uid": user_id}
    if realm == "tenant":
        if tenant_id is None:
            raise ValueError("租户角色查询必须提供 tenant_id")
        statement = text(
            "select r.code from user_roles ur "
            "join roles r on r.id = ur.role_id and r.tenant_id = ur.tenant_id "
            "where ur.tenant_id = :tid and ur.user_id = :uid"
        )
        params["tid"] = tenant_id
    else:
        statement = text(
            "select r.code from platform_user_roles ur "
            "join platform_roles r on r.id = ur.platform_role_id "
            "where ur.platform_user_id = :uid"
        )
    codes: list[str] = list((await session.execute(statement, params)).scalars().all())
    return frozenset(str(code) for code in codes)


def _scope_params(realm: Realm, tenant_id: UUID | None) -> dict[str, UUID]:
    """租户 SQL 必须显式携带 tenant_id（RLS 之外的第二道过滤）。"""

    if realm == "tenant":
        if tenant_id is None:
            raise ValueError("租户查询必须提供 tenant_id")
        return {"tid": tenant_id}
    return {}


async def _revoke_family(
    session: AsyncSession,
    *,
    realm: Realm,
    tenant_id: UUID | None,
    user_id: UUID,
    family_id: UUID,
    reason: str,
) -> bool:
    """撤销一个刷新令牌家族（幂等）；返回是否实际更新了活跃行。"""

    updated = (
        await session.execute(
            text(_SQL[realm]["revoke_family"]),
            {
                "reason": reason,
                "family_id": family_id,
                "uid": user_id,
                **_scope_params(realm, tenant_id),
            },
        )
    ).first()
    return updated is not None


async def login_tenant(
    *,
    school_code: str,
    login_id: str,
    password: str,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> TokenPair:
    """校方 B 端 / C 端登录（学校编号 + 学号/工号 + 密码）。

    失败路径：先在事务内判定，**退出事务后**单独写失败审计再抛错（否则审计会随回滚丢失）。

    异常:
        AuthError: 凭证错误（401，文案统一）或租户/账号不可用。
    """

    settings = get_settings()
    code = school_code.strip().upper()
    login = login_id.strip()
    ip_hash = hash_client_fingerprint(ip)
    ua_hash = hash_client_fingerprint(user_agent)
    device_label = session_store.device_label_from_user_agent(user_agent)

    async with db_session(platform=True) as platform_session:
        tenant_row = (
            await platform_session.execute(
                text("select id, status from tenants where school_code = :code"),
                {"code": code},
            )
        ).mappings().one_or_none()

    if tenant_row is None or tenant_row["status"] != "active":
        async with db_session(platform=True) as audit_session:
            await append_audit(
                audit_session,
                action="auth.login.failed",
                resource_type="auth",
                actor_realm="system",
                result="failure",
                metadata={"school_code": code, "reason": "tenant_unavailable"},
                request_id=request_id,
            )
        raise _invalid_credentials()

    tenant_id: UUID = tenant_row["id"]
    pair: TokenPair | None = None
    kicked: list[UUID] = []

    async with db_session(tenant_id=tenant_id) as session:
        # 先取 id 再加锁，锁后重读：避免并发改密/停用后签发旧 token_version
        user_id_row = (
            await session.execute(
                text("select id from users where tenant_id = :tid and login_id = :login"),
                {"tid": tenant_id, "login": login},
            )
        ).first()
        if user_id_row is not None:
            await session_store.acquire_user_lock(
                session, realm="tenant", user_id=user_id_row[0]
            )

        user_row = (
            await session.execute(
                text(
                    "select id, status, password_hash, token_version, must_change_password "
                    "from users where tenant_id = :tid and login_id = :login"
                ),
                {"tid": tenant_id, "login": login},
            )
        ).mappings().one_or_none()

        stored_hash = str(user_row["password_hash"]) if user_row is not None else _TIMING_EQUALIZATION_HASH
        password_ok = False
        if _password_within_bcrypt_limit(password):
            # 账号不存在时同样比对占位哈希：失败路径耗时一致（反账号枚举）
            password_ok = await anyio.to_thread.run_sync(verify_password, password, stored_hash)

        roles: frozenset[str] = frozenset()
        if user_row is not None and user_row["status"] == "active":
            roles = await _load_roles(
                session, realm="tenant", tenant_id=tenant_id, user_id=user_row["id"]
            )

        if user_row is not None and password_ok and user_row["status"] == "active":
            kicked = await session_store.enforce_limit(
                session,
                realm="tenant",
                tenant_id=tenant_id,
                user_id=user_row["id"],
                limit=settings.max_sessions_per_user_tenant,
                request_id=request_id,
            )
            pair = await _issue_tokens(
                settings,
                realm="tenant",
                user_id=user_row["id"],
                tenant_id=tenant_id,
                token_version=int(user_row["token_version"]),
                must_change_password=bool(user_row["must_change_password"]),
                ip_hash=ip_hash,
                ua_hash=ua_hash,
                session=session,
                device_label=device_label,
                roles=roles,
            )
            await append_audit(
                session,
                action="auth.login.success",
                resource_type="auth",
                actor_realm="tenant",
                actor_id=user_row["id"],
                tenant_id=tenant_id,
                metadata={
                    "login_masked": login[:1] + "***",
                    "ip_hash": ip_hash,
                    "ua_hash": ua_hash,
                },
                request_id=request_id,
            )
            # 标记在事务内登记：撤销方串行化后不会再被"提交后补写"复活
            await revocation.register_session(pair.session_id, realm="tenant")

    if pair is None:
        async with db_session(tenant_id=tenant_id) as audit_session:
            await append_audit(
                audit_session,
                action="auth.login.failed",
                resource_type="auth",
                actor_realm="tenant",
                tenant_id=tenant_id,
                result="failure",
                metadata={
                    "login_masked": login[:1] + "***",
                    "ip_hash": ip_hash,
                    "ua_hash": ua_hash,
                },
                request_id=request_id,
            )
        raise _invalid_credentials()

    # 事务已提交（新标记已在事务内登记）：清理被踢设备的标记；失败只告警，
    # 这些 family 已在数据库撤销，刷新令牌无法再用。
    await revocation.delete_sessions(kicked, realm="tenant")
    return pair


async def login_platform(
    *,
    account: str,
    password: str,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> TokenPair:
    """平台账号登录。

    异常:
        AuthError: 凭证错误（401，文案统一）。
    """

    settings = get_settings()
    normalized = account.strip()
    ip_hash = hash_client_fingerprint(ip)
    ua_hash = hash_client_fingerprint(user_agent)
    device_label = session_store.device_label_from_user_agent(user_agent)
    pair: TokenPair | None = None
    kicked: list[UUID] = []

    async with db_session(platform=True) as session:
        user_id_row = (
            await session.execute(
                text("select id from platform_users where account = :account"),
                {"account": normalized},
            )
        ).first()
        if user_id_row is not None:
            await session_store.acquire_user_lock(
                session, realm="platform", user_id=user_id_row[0]
            )

        row = (
            await session.execute(
                text(
                    "select id, status, password_hash, token_version, must_change_password "
                    "from platform_users where account = :account"
                ),
                {"account": normalized},
            )
        ).mappings().one_or_none()

        stored_hash = str(row["password_hash"]) if row is not None else _TIMING_EQUALIZATION_HASH
        password_ok = False
        if _password_within_bcrypt_limit(password):
            password_ok = await anyio.to_thread.run_sync(verify_password, password, stored_hash)

        roles: frozenset[str] = frozenset()
        if row is not None and row["status"] == "active":
            roles = await _load_roles(
                session, realm="platform", tenant_id=None, user_id=row["id"]
            )

        if row is not None and password_ok and row["status"] == "active":
            kicked = await session_store.enforce_limit(
                session,
                realm="platform",
                tenant_id=None,
                user_id=row["id"],
                limit=settings.max_sessions_per_user_platform,
                request_id=request_id,
            )
            pair = await _issue_tokens(
                settings,
                realm="platform",
                user_id=row["id"],
                tenant_id=None,
                token_version=int(row["token_version"]),
                must_change_password=bool(row["must_change_password"]),
                ip_hash=ip_hash,
                ua_hash=ua_hash,
                session=session,
                device_label=device_label,
                roles=roles,
            )
            await append_audit(
                session,
                action="auth.login.success",
                resource_type="auth",
                actor_realm="platform",
                actor_id=row["id"],
                metadata={"realm": "platform", "ip_hash": ip_hash, "ua_hash": ua_hash},
                request_id=request_id,
            )
            await revocation.register_session(pair.session_id, realm="platform")

    if pair is None:
        async with db_session(platform=True) as audit_session:
            await append_audit(
                audit_session,
                action="auth.login.failed",
                resource_type="auth",
                actor_realm="platform",
                result="failure",
                metadata={
                    "realm": "platform",
                    "account_masked": normalized[:1] + "***",
                    "ip_hash": ip_hash,
                    "ua_hash": ua_hash,
                },
                request_id=request_id,
            )
        raise _invalid_credentials()

    await revocation.delete_sessions(kicked, realm="platform")
    return pair


async def refresh_tokens(
    *,
    refresh_token: str,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> TokenPair:
    """刷新访问令牌（轮换 + 重用检测）。

    失败路径（未知令牌/已撤销/过期/重用/账号不可用）在回滚后的**独立事务**落库：
    重用检测会撤销整个家族（`reuse_detected`），所有失败写 `auth.refresh.*` 审计。

    异常:
        AuthError: 令牌无效/过期/被撤销/重用（401）。
    """

    settings = get_settings()
    realm, tenant_id, jti = parse_refresh_token(refresh_token)
    expected_hash = hash_refresh_token(refresh_token.strip())
    ip_hash = hash_client_fingerprint(ip)
    ua_hash = hash_client_fingerprint(user_agent)
    device_label = session_store.device_label_from_user_agent(user_agent)

    try:
        if realm == "tenant":
            if tenant_id is None:
                raise AuthError("INVALID_REFRESH_TOKEN", "刷新令牌无效", 401)
            async with db_session(tenant_id=tenant_id) as session:
                pair = await _rotate(
                    settings,
                    session=session,
                    realm=realm,
                    tenant_id=tenant_id,
                    jti=jti,
                    expected_hash=expected_hash,
                    ip_hash=ip_hash,
                    ua_hash=ua_hash,
                    device_label=device_label,
                    request_id=request_id,
                )
        else:
            async with db_session(platform=True) as session:
                pair = await _rotate(
                    settings,
                    session=session,
                    realm=realm,
                    tenant_id=None,
                    jti=jti,
                    expected_hash=expected_hash,
                    ip_hash=ip_hash,
                    ua_hash=ua_hash,
                    device_label=device_label,
                    request_id=request_id,
                )
    except _RefreshFailure as failure:
        await _record_refresh_failure(
            realm=realm,
            tenant_id=tenant_id,
            failure=failure,
            request_id=request_id,
            ip_hash=ip_hash,
            ua_hash=ua_hash,
        )
        raise _refresh_error(failure.reason) from failure

    return pair


async def _tenant_exists(tenant_id: UUID) -> bool:
    """租户是否存在（平台连接查询，避免在租户会话内嵌套第二连接）。"""

    async with db_session(platform=True) as session:
        return bool(await session.scalar(text("select 1 from tenants where id = :tid"), {"tid": tenant_id}))


async def _record_refresh_failure(
    *,
    realm: Realm,
    tenant_id: UUID | None,
    failure: _RefreshFailure,
    request_id: str | None,
    ip_hash: str | None = None,
    ua_hash: str | None = None,
) -> None:
    """在独立事务里记录刷新失败（必要时撤销家族）。"""

    if realm == "tenant" and tenant_id is not None and not await _tenant_exists(tenant_id):
        # 调用方伪造/陈旧 tenant_id：审计降级为平台事件，避免外键违约（审查 MA3）
        await _record_failure_as_platform(
            failure=failure,
            tenant_id=tenant_id,
            request_id=request_id,
            ip_hash=ip_hash,
            ua_hash=ua_hash,
        )
        return

    if (
        failure.reason == "reuse"
        and failure.family_id is not None
        and failure.user_id is not None
    ):
        # 先独立提交家族撤销（安全动作优先），审计失败不得连带回滚（审查 Minor 9）；
        # 取同一把用户锁，避免在途刷新插入的新行逃过撤销（第二轮审查 M3）。
        async with session_store.session_scope(realm=realm, tenant_id=tenant_id) as session:
            await session_store.acquire_user_lock(
                session, realm=realm, user_id=failure.user_id
            )
            await _revoke_family(
                session,
                realm=realm,
                tenant_id=tenant_id,
                user_id=failure.user_id,
                family_id=failure.family_id,
                reason="reuse_detected",
            )
        # 撤销提交后删除会话标记，令该家族的访问令牌下一次请求立即 401
        await revocation.delete_session(failure.family_id, realm=realm)

    if failure.reason == "reuse" and failure.family_id is not None:
        async with session_store.session_scope(realm=realm, tenant_id=tenant_id) as audit_session:
            await append_audit(
                audit_session,
                action="auth.refresh.reuse_detected",
                resource_type="auth",
                actor_realm=realm,
                actor_id=failure.user_id,
                tenant_id=tenant_id,
                result="failure",
                metadata={
                    "family_id": str(failure.family_id),
                    "ip_hash": ip_hash,
                    "ua_hash": ua_hash,
                },
                request_id=request_id,
            )
        return

    async with session_store.session_scope(realm=realm, tenant_id=tenant_id) as session:
        await append_audit(
            session,
            action="auth.refresh.failed",
            resource_type="auth",
            actor_realm=realm,
            actor_id=failure.user_id,
            tenant_id=tenant_id,
            result="failure",
            metadata={
                "reason": failure.reason,
                "ip_hash": ip_hash,
                "ua_hash": ua_hash,
            },
            request_id=request_id,
        )


async def _record_failure_as_platform(
    *,
    failure: _RefreshFailure,
    tenant_id: UUID | None,
    request_id: str | None,
    ip_hash: str | None = None,
    ua_hash: str | None = None,
) -> None:
    """租户不可用时的失败审计（平台连接、tenant_id 为空，保留被尝试的租户 id 供排障）。"""

    async with db_session(platform=True) as session:
        await append_audit(
            session,
            action="auth.refresh.failed",
            resource_type="auth",
            actor_realm="system",
            result="failure",
            metadata={
                "reason": failure.reason,
                "attempted_tenant_id": str(tenant_id) if tenant_id else None,
                "ip_hash": ip_hash,
                "ua_hash": ua_hash,
            },
            request_id=request_id,
        )


def _refresh_error(reason: str) -> AuthError:
    """刷新失败原因 → 业务错误。"""

    if reason == "reuse":
        return AuthError("REFRESH_REUSE_DETECTED", "登录状态异常，请重新登录", 401)
    if reason == "expired":
        return AuthError("REFRESH_TOKEN_EXPIRED", "登录已过期，请重新登录", 401)
    if reason == "account_unavailable":
        return AuthError("ACCOUNT_UNAVAILABLE", "账号不可用，请联系管理员", 401)
    if reason == "revoked":
        return AuthError("INVALID_REFRESH_TOKEN", "登录已失效，请重新登录", 401)
    return AuthError("INVALID_REFRESH_TOKEN", "刷新令牌无效，请重新登录", 401)


async def _revoked_failure(
    session: AsyncSession,
    *,
    realm: Realm,
    tenant_id: UUID | None,
    row: Any,
) -> _RefreshFailure:
    """已撤销令牌的失败信号：只有"已轮换令牌被重放"才算 reuse。

    被踢/下线/改密会撤销整个 family（含已轮换的旧行），此时重放旧令牌属于
    已知撤销而非泄漏信号，按 revoked 返回 401，避免误报盗用（简报 §4 第 7 条）。
    仅看当前行会漏掉"该行早已 rotated、家族后来被踢"的情形，故再查家族是否
    存在非 rotated 的撤销行。自然过期后重放的旧令牌也不再构成泄漏信号。
    """

    reuse = (
        row["revoked_reason"] == "rotated"
        and row["rotated_to_jti"] is not None
        and row["expires_at"] > datetime.now(UTC)
    )
    if reuse:
        params: dict[str, object] = {"fid": row["family_id"], "uid": row["user_id"]}
        params.update(_scope_params(realm, tenant_id))
        family_revoked = await session.scalar(
            text(_SQL[realm]["family_revoked"]), params
        )
        if family_revoked is not None:
            reuse = False
    return _RefreshFailure(
        "reuse" if reuse else "revoked",
        family_id=row["family_id"],
        user_id=row["user_id"],
    )


async def _rotate(
    settings: Settings,
    *,
    session: AsyncSession,
    realm: Realm,
    tenant_id: UUID | None,
    jti: UUID,
    expected_hash: str,
    ip_hash: str | None,
    ua_hash: str | None,
    device_label: str | None,
    request_id: str | None,
) -> TokenPair:
    """在指定连接/事务内完成一次刷新轮换。

    业务失败通过 `_RefreshFailure` 抛出（**不在此事务内写审计**），由
    `refresh_tokens` 在回滚后的独立事务里落库，保证审计与家族撤销一定被提交。

    异常:
        _RefreshFailure: 未知/已撤销/过期/重用/账号不可用。
    """

    row = (
        await session.execute(
            text(_SQL[realm]["select_refresh"]),
            {"jti": jti, **_scope_params(realm, tenant_id)},
        )
    ).mappings().one_or_none()

    if row is None or not hmac.compare_digest(str(row["token_hash"]), expected_hash):
        raise _RefreshFailure("unknown_token")

    # 与撤销/改密/其它刷新串行化；锁后重读，避免用旧快照轮换已撤销 family
    await session_store.acquire_user_lock(session, realm=realm, user_id=row["user_id"])
    row = (
        await session.execute(
            text(_SQL[realm]["select_refresh"]),
            {"jti": jti, **_scope_params(realm, tenant_id)},
        )
    ).mappings().one_or_none()
    if row is None or not hmac.compare_digest(str(row["token_hash"]), expected_hash):
        raise _RefreshFailure("unknown_token")

    if row["revoked_at"] is not None:
        raise await _revoked_failure(
            session, realm=realm, tenant_id=tenant_id, row=row
        )

    if row["expires_at"] <= datetime.now(UTC):
        raise _RefreshFailure("expired", family_id=row["family_id"], user_id=row["user_id"])

    user_row = (
        await session.execute(
            text(_SQL[realm]["select_user_state"]),
            {"uid": row["user_id"], **_scope_params(realm, tenant_id)},
        )
    ).mappings().one_or_none()
    if user_row is None or user_row["status"] != "active":
        raise _RefreshFailure(
            "account_unavailable", family_id=row["family_id"], user_id=row["user_id"]
        )

    roles = await _load_roles(
        session, realm=realm, tenant_id=tenant_id, user_id=row["user_id"]
    )
    # 无 UA 的客户端刷新时保留原设备标签，避免把可识别设备降级成"未知设备"
    effective_label = (
        str(row["device_label"])
        if device_label == session_store.UNKNOWN_DEVICE and row["device_label"]
        else device_label
    )

    new_jti = uuid4()
    new_refresh_token = _build_refresh_token(realm, new_jti, tenant_id)
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.refresh_token_ttl_seconds)

    # 条件更新：并发下只有一个请求能把旧令牌标记为"已轮换"
    rotated = (
        await session.execute(
            text(_SQL[realm]["rotate_refresh"]),
            {"new_jti": new_jti, "id": row["id"], **_scope_params(realm, tenant_id)},
        )
    ).first()
    if rotated is None:
        # 锁后仍失败：重读原因。被踢/下线/改密竞争输者按"已撤销"返回，
        # 只有真正 replay 已轮换令牌才按重用检测处理（简报 §4 第 7 条）。
        current = (
            await session.execute(
                text(_SQL[realm]["select_refresh"]),
                {"jti": jti, **_scope_params(realm, tenant_id)},
            )
        ).mappings().one_or_none()
        if current is None:
            raise _RefreshFailure("unknown_token")
        raise await _revoked_failure(
            session, realm=realm, tenant_id=tenant_id, row=current
        )

    params: dict[str, object] = {
        "user_id": row["user_id"],
        "jti": new_jti,
        "family_id": row["family_id"],
        "token_hash": hash_refresh_token(new_refresh_token),
        "expires_at": expires_at,
        "last_seen_at": now,
        "device_label": effective_label,
        "ua_hash": ua_hash,
        "ip_hash": ip_hash,
    }
    if realm == "tenant":
        params["tenant_id"] = tenant_id
    await session.execute(text(_refresh_insert_sql(realm)), params)

    access_token, _ = create_access_token(
        settings,
        user_id=row["user_id"],
        realm=realm,
        tenant_id=tenant_id,
        family_id=row["family_id"],
        token_version=int(user_row["token_version"]),
        must_change_password=bool(user_row["must_change_password"]),
    )
    await append_audit(
        session,
        action="auth.refresh.success",
        resource_type="auth",
        actor_realm=realm,
        actor_id=row["user_id"],
        tenant_id=tenant_id,
        metadata={
            "family_id": str(row["family_id"]),
            "ip_hash": ip_hash,
            "ua_hash": ua_hash,
        },
        request_id=request_id,
    )
    # 事务内续期标记：若并发撤销已提交，撤销方持有同一把锁并会随后删除标记
    await revocation.register_session(row["family_id"], realm=realm)
    return TokenPair(
        access_token=access_token,
        refresh_token=new_refresh_token,
        expires_in=settings.access_token_ttl_seconds,
        realm=realm,
        user_id=row["user_id"],
        tenant_id=tenant_id,
        session_id=row["family_id"],
        must_change_password=bool(user_row["must_change_password"]),
        roles=roles,
    )


async def revoke_session_by_id(
    *,
    realm: Realm,
    tenant_id: UUID | None,
    user_id: UUID,
    session_id: UUID,
) -> bool:
    """按当前访问令牌的 sid 撤销家族（无刷新令牌时的登出兜底）。

    调用方已通过鉴权；SQL 仍按 user_id + tenant_id 过滤。审计由调用方的
    `record_logout` 统一写入，避免一次登出产生两条同义事件。
    """

    async with session_store.session_scope(realm=realm, tenant_id=tenant_id) as session:
        await session_store.acquire_user_lock(session, realm=realm, user_id=user_id)
        revoked = await _revoke_family(
            session,
            realm=realm,
            tenant_id=tenant_id,
            user_id=user_id,
            family_id=session_id,
            reason="logout",
        )
    if revoked:
        await revocation.delete_session(session_id, realm=realm)
    return revoked


async def record_logout(
    *,
    actor_user_id: UUID,
    actor_realm: Realm,
    tenant_id: UUID | None,
    family_id: UUID | None = None,
    family_revoked: bool = False,
    access_revoked: bool = True,
    subject_user_id: UUID | None = None,
    request_id: str | None = None,
    ip_hash: str | None = None,
    ua_hash: str | None = None,
    scope_realm: Realm | None = None,
) -> None:
    """记录一次登出审计（无论是否携带刷新令牌）。

    `scope_realm`/`tenant_id` 指向**被撤销 family 的归属**（可跨租/跨 realm）；
    缺省与操作者 realm 一致。审计行落在归属租户，`actor_realm` 始终是操作者。
    """

    audit_realm = scope_realm or actor_realm
    async with session_store.session_scope(realm=audit_realm, tenant_id=tenant_id) as session:
        await append_audit(
            session,
            action="auth.logout",
            resource_type="auth",
            actor_realm=actor_realm,
            actor_id=actor_user_id,
            tenant_id=tenant_id if audit_realm == "tenant" else None,
            metadata={
                "access_revoked": access_revoked,
                "family_id": str(family_id) if family_id is not None else None,
                "family_revoked": family_revoked,
                "subject_user_id": (
                    str(subject_user_id) if subject_user_id is not None else None
                ),
                "ip_hash": ip_hash,
                "ua_hash": ua_hash,
            },
            request_id=request_id,
        )


async def revoke_refresh_family(
    *,
    refresh_token: str,
    actor_user_id: UUID | None,
    actor_realm: Realm | None,
    request_id: str | None = None,
    audit: bool = True,
) -> FamilyRevocation | None:
    """登出：撤销该刷新令牌所属家族。

    **必须校验刷新令牌的 secret 哈希**（与刷新链路同标准）：只凭 jti 会被第三方登出他人，
    且审计会把操作者记成受害者（审查 MA1）。哈希不匹配或令牌不存在时静默返回 None。

    `audit=False` 供同时携带 Bearer 的登出复用：由 `record_logout` 写带完整结果字段的
    统一事件，避免同一 family 重复审计。

    返回值:
        family_id 与"本次是否实际撤销"；无效令牌返回 None。调用方据此判断是否涉及
        第二个 family、以及审计里的 `family_revoked` 是否为真。
    """

    try:
        realm, tenant_id, jti = parse_refresh_token(refresh_token)
    except AuthError:
        return None
    expected_hash = hash_refresh_token(refresh_token.strip())
    if realm == "tenant":
        if tenant_id is None:
            return None
        async with db_session(tenant_id=tenant_id) as session:
            revocation_result = await _revoke_by_jti(
                session,
                realm=realm,
                tenant_id=tenant_id,
                jti=jti,
                expected_hash=expected_hash,
                reason="logout",
                actor_user_id=actor_user_id,
                actor_realm=actor_realm,
                request_id=request_id,
                audit=audit,
            )
    else:
        async with db_session(platform=True) as session:
            revocation_result = await _revoke_by_jti(
                session,
                realm=realm,
                tenant_id=None,
                jti=jti,
                expected_hash=expected_hash,
                reason="logout",
                actor_user_id=actor_user_id,
                actor_realm=actor_realm,
                request_id=request_id,
                audit=audit,
            )
    if revocation_result is None:
        return None
    await revocation.delete_session(revocation_result.family_id, realm=realm)
    return revocation_result


async def _revoke_by_jti(
    session: AsyncSession,
    *,
    realm: Realm,
    tenant_id: UUID | None,
    jti: UUID,
    expected_hash: str,
    reason: str,
    actor_user_id: UUID | None,
    actor_realm: Realm | None,
    request_id: str | None,
    audit: bool = True,
) -> FamilyRevocation | None:
    """按 jti 撤销所在家族（校验令牌哈希；审计记录真实操作者）。

    `audit=False` 时只撤销、不写事件（调用方负责写带完整结果字段的统一登出事件）。

    返回值:
        family_id 与"本次是否实际撤销"（此前已被踢/下线时为 False）；
        令牌不存在/哈希不匹配返回 None。
    """

    row = (
        await session.execute(
            text(_SQL[realm]["family_by_jti"]),
            {"jti": jti, **_scope_params(realm, tenant_id)},
        )
    ).mappings().one_or_none()
    if row is None or not hmac.compare_digest(str(row["token_hash"]), expected_hash):
        return None

    # 与刷新串行化后重读：避免登出撤销与轮换交错时漏掉新插入的行
    await session_store.acquire_user_lock(session, realm=realm, user_id=row["user_id"])
    row = (
        await session.execute(
            text(_SQL[realm]["family_by_jti"]),
            {"jti": jti, **_scope_params(realm, tenant_id)},
        )
    ).mappings().one_or_none()
    if row is None or not hmac.compare_digest(str(row["token_hash"]), expected_hash):
        return None

    family_revoked = await _revoke_family(
        session,
        realm=realm,
        tenant_id=tenant_id,
        user_id=row["user_id"],
        family_id=row["family_id"],
        reason=reason,
    )
    if audit:
        await append_audit(
            session,
            action="auth.logout",
            resource_type="auth",
            actor_realm=actor_realm or realm,
            actor_id=actor_user_id or row["user_id"],
            tenant_id=tenant_id,
            metadata={
                "family_id": str(row["family_id"]),
                "subject_user_id": str(row["user_id"]),
                "family_revoked": family_revoked,
                # 无 Bearer 主体（仅 Cookie 登出）时标注，便于审计区分"本人操作"与"凭 Cookie 撤销"
                "self_revoked": actor_user_id is None,
            },
            request_id=request_id,
        )
    return FamilyRevocation(
        cast(UUID, row["family_id"]),
        family_revoked,
        realm,
        tenant_id,
        cast(UUID, row["user_id"]),
    )


async def change_password(
    *,
    principal_user_id: UUID,
    realm: Realm,
    tenant_id: UUID | None,
    current_password: str,
    new_password: str,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> TokenPair:
    """修改密码：校验旧密码 → 更新哈希 → 撤销全部家族 → 重新签发令牌。

    异常:
        AuthError: 旧密码错误（401）、新密码不合规（422）、账号不可用（401）。
    """

    if not (MIN_PASSWORD_LENGTH <= len(new_password) <= MAX_PASSWORD_LENGTH):
        raise AuthError(
            "WEAK_PASSWORD",
            f"新密码长度需在 {MIN_PASSWORD_LENGTH}~{MAX_PASSWORD_LENGTH} 之间",
            422,
        )
    if new_password == current_password:
        raise AuthError("WEAK_PASSWORD", "新密码不能与当前密码相同", 422)
    if not _password_within_bcrypt_limit(new_password):
        raise AuthError(
            "WEAK_PASSWORD",
            f"新密码不能超过 {MAX_PASSWORD_BYTES} 字节（一个汉字约 3 字节）",
            422,
        )

    settings = get_settings()
    ip_hash = hash_client_fingerprint(ip)
    ua_hash = hash_client_fingerprint(user_agent)
    device_label = session_store.device_label_from_user_agent(user_agent)

    try:
        async with session_store.session_scope(realm=realm, tenant_id=tenant_id) as session:
            pair, old_families = await _change_password_in_session(
                session,
                settings=settings,
                realm=realm,
                tenant_id=tenant_id,
                principal_user_id=principal_user_id,
                current_password=current_password,
                new_password=new_password,
                ip_hash=ip_hash,
                ua_hash=ua_hash,
                device_label=device_label,
                request_id=request_id,
            )
    except _RefreshFailure as failure:
        await _record_password_failure(
            realm=realm,
            tenant_id=tenant_id,
            user_id=principal_user_id,
            reason=failure.reason,
            request_id=request_id,
            ip_hash=ip_hash,
            ua_hash=ua_hash,
        )
        if failure.reason == "password_mismatch":
            raise AuthError("INVALID_CREDENTIALS", "当前密码不正确", 401) from failure
        raise AuthError("ACCOUNT_UNAVAILABLE", "账号不可用，请联系管理员", 401) from failure

    # 事务已提交（新标记已在事务内登记）：删除全部旧设备标记
    await revocation.delete_sessions(old_families, realm=realm)
    return pair


async def _record_password_failure(
    *,
    realm: Realm,
    tenant_id: UUID | None,
    user_id: UUID,
    reason: str,
    request_id: str | None,
    ip_hash: str | None = None,
    ua_hash: str | None = None,
) -> None:
    """改密失败审计（独立事务，避免随回滚丢失）。"""

    async with session_store.session_scope(realm=realm, tenant_id=tenant_id) as session:
        await append_audit(
            session,
            action="auth.password.change_failed",
            resource_type="auth",
            actor_realm=realm,
            actor_id=user_id,
            tenant_id=tenant_id,
            result="failure",
            metadata={"reason": reason, "ip_hash": ip_hash, "ua_hash": ua_hash},
            request_id=request_id,
        )


async def _change_password_in_session(
    session: AsyncSession,
    *,
    settings: Settings,
    realm: Realm,
    tenant_id: UUID | None,
    principal_user_id: UUID,
    current_password: str,
    new_password: str,
    ip_hash: str | None,
    ua_hash: str | None,
    device_label: str | None,
    request_id: str | None,
) -> tuple[TokenPair, list[UUID]]:
    """改密主体逻辑（同一事务内：改哈希、自增版本、撤销全部家族、重签令牌）。

    返回值:
        (新令牌, 被撤销的旧 family id 列表)；后者用于事务提交后清理 Redis 标记。
    """

    await session_store.acquire_user_lock(
        session, realm=realm, user_id=principal_user_id
    )
    row = (
        await session.execute(
            text(_SQL[realm]["select_credentials"]),
            {"uid": principal_user_id, **_scope_params(realm, tenant_id)},
        )
    ).mappings().one_or_none()
    if row is None or row["status"] != "active":
        raise AuthError("ACCOUNT_UNAVAILABLE", "账号不可用，请联系管理员", 401)

    old_ok = await anyio.to_thread.run_sync(
        verify_password, current_password, str(row["password_hash"])
    )
    if not old_ok:
        # 失败审计在回滚后由外层单独写：此处抛信号，不落库（否则会被回滚）
        raise _RefreshFailure("password_mismatch", user_id=principal_user_id)

    new_hash = await anyio.to_thread.run_sync(hash_password, new_password)
    await session.execute(
        text(_SQL[realm]["set_credential_hash"]),
        {"hash": new_hash, "uid": principal_user_id, **_scope_params(realm, tenant_id)},
    )
    old_families: list[UUID] = list(
        (
            await session.execute(
                text(_SQL[realm]["active_families"]),
                {"uid": principal_user_id, **_scope_params(realm, tenant_id)},
            )
        )
        .scalars()
        .all()
    )
    await session.execute(
        text(_SQL[realm]["revoke_user_tokens"]),
        {"uid": principal_user_id, **_scope_params(realm, tenant_id)},
    )

    version_row: int = (
        await session.execute(
            text(_SQL[realm]["select_credential_version"]),
            {"uid": principal_user_id, **_scope_params(realm, tenant_id)},
        )
    ).scalar_one()

    roles = await _load_roles(
        session, realm=realm, tenant_id=tenant_id, user_id=principal_user_id
    )
    pair = await _issue_tokens(
        settings,
        realm=realm,
        user_id=principal_user_id,
        tenant_id=tenant_id,
        token_version=int(version_row),
        must_change_password=False,
        ip_hash=ip_hash,
        ua_hash=ua_hash,
        session=session,
        device_label=device_label,
        roles=roles,
    )
    await append_audit(
        session,
        action="auth.password.changed",
        resource_type="auth",
        actor_realm=realm,
        actor_id=principal_user_id,
        tenant_id=tenant_id,
        metadata={"sessions_revoked": True, "ip_hash": ip_hash, "ua_hash": ua_hash},
        request_id=request_id,
    )
    await revocation.register_session(pair.session_id, realm=realm)
    return pair, old_families
