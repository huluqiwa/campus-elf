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
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote
from uuid import UUID, uuid4

import anyio
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import append_audit
from app.auth import revocation
from app.auth import sessions as session_store
from app.auth.lockout import (
    LockoutState,
    is_account_locked,
    register_login_failure,
)
from app.auth.mfa import (
    MFA_INVALID_MESSAGE,
    create_mfa_token,
    decode_mfa_token,
)
from app.auth.passwords import (
    MAX_PASSWORD_BYTES,
    PasswordPolicyError,
    hash_password,
    is_password_reused,
    validate_password_strength,
    verify_password,
)
from app.auth.tokens import (
    Realm,
    TokenError,
    create_access_token,
    generate_password_reset_token,
    generate_refresh_token,
    hash_client_fingerprint,
    hash_password_reset_token,
    hash_refresh_token,
    parse_password_reset_token,
)
from app.config import Settings, get_settings
from app.core.metrics import record_login_failure
from app.db.session import db_session
from app.integrations.mail import build_mailer, render_password_reset_email
from app.security.encryption import (
    SecretDecryptError,
    SecretEncryptionError,
    decode_kek,
    decrypt_secret,
    encrypt_secret,
    generate_recovery_codes,
    hash_recovery_code,
    mfa_aad,
    recovery_code_subkey,
)
from app.security.totp import generate_totp_secret, totp_uri, verify_totp

logger = logging.getLogger(__name__)

# bcrypt 输入上限 72 字节（多字节按 UTF-8 计）：超长会抛 ValueError（500）。
# 入口按字节拦截；改哈希方案（argon2/预哈希）见 TD-107。


# 刷新令牌串的两类前缀（用于区分 realm；secret 才是凭证）
_TENANT_REFRESH_KIND = "v1t"
_PLATFORM_REFRESH_KIND = "v1p"

INVALID_CREDENTIALS_MESSAGE = "学校编号、账号或密码不正确"

# 账号不存在时用于"等时假比对"的占位摘要（bcrypt cost 12，与真实校验同耗时）：
# 登录失败路径统一付出一次 bcrypt 开销，避免用响应时间枚举账号是否存在（审查 Minor 6）。
# 该值不是任何账号的口令，仅用于消耗等价计算量。
_TIMING_EQUALIZATION_HASH = "$2b$12$lmaZFg4Ynv/J4PEzLkCYlOkUhUL6H2ffvrAmrTHkoc14Q8Q.PuDEm"

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
            "revoked_reason, rotated_to_jti, device_label, mfa_verified from refresh_tokens "
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
            "select status, password_hash, login_id, failed_login_attempts, "
            "failed_login_window_started_at, locked_until from users "
            "where tenant_id = :tid and id = :uid"
        ),
        "set_credential_hash": (
            "update users set password_hash = :hash, must_change_password = false, "
            "password_changed_at = now(), token_version = token_version + 1, "
            "failed_login_attempts = 0, failed_login_window_started_at = null, "
            "locked_until = null, updated_at = now() "
            "where tenant_id = :tid and id = :uid"
        ),
        "select_login": (
            "select id, status, password_hash, token_version, must_change_password, "
            "mfa_enabled, failed_login_attempts, failed_login_window_started_at, locked_until "
            "from users where tenant_id = :tid and login_id = :login"
        ),
        "update_login_failure": (
            "update users set failed_login_attempts = :attempts, "
            "failed_login_window_started_at = :window_started, "
            "locked_until = :locked_until, updated_at = now() "
            "where tenant_id = :tid and id = :uid"
        ),
        "clear_login_failures": (
            "update users set failed_login_attempts = 0, "
            "failed_login_window_started_at = null, locked_until = null, updated_at = now() "
            "where tenant_id = :tid and id = :uid "
            "and (failed_login_attempts <> 0 or failed_login_window_started_at is not null "
            "     or locked_until is not null)"
        ),
        "select_password_history": (
            "select password_hash from password_history "  # nosec B105 - SQL 文本，非口令
            "where tenant_id = :tid and user_id = :uid "
            "order by created_at desc, id desc limit :limit"
        ),
        "insert_password_history": (
            "insert into password_history (tenant_id, user_id, password_hash) "  # nosec B105
            "values (:tid, :uid, :hash)"
        ),
        "trim_password_history": (
            "delete from password_history "  # nosec B105 - SQL 文本，非口令
            "where tenant_id = :tid and user_id = :uid and id not in ("
            "  select id from password_history "
            "  where tenant_id = :tid and user_id = :uid "
            "  order by created_at desc, id desc limit :keep"
            ")"
        ),
        "revoke_user_tokens": (
            "update refresh_tokens set revoked_at = now(), revoked_reason = :reason "
            "where tenant_id = :tid and user_id = :uid and revoked_at is null"
        ),
        "select_credential_version": (
            "select token_version from users where tenant_id = :tid and id = :uid"
        ),
        "active_families": (
            # 只数**还活着**的家族（未撤销且未过期）：`sessions_revoked` 的口径统一为
            # 「本次真正断开的活跃会话家族数」，与停用侧、设备列表取数一致（TD-231）。
            "select distinct family_id from refresh_tokens "
            "where tenant_id = :tid and user_id = :uid "
            "and revoked_at is null and expires_at > now()"
        ),
        "select_reset_user_by_login": (
            "select id, status, email, user_type from users "
            "where tenant_id = :tid and login_id = :login"
        ),
        "latest_reset_created_at": (
            "select created_at from password_reset_tokens "
            "where tenant_id = :tid and user_id = :uid "
            "order by created_at desc, id desc limit 1"
        ),
        "invalidate_reset_tokens": (
            "update password_reset_tokens set used_at = now() "  # nosec B105 - SQL 文本，非口令
            "where tenant_id = :tid and user_id = :uid and used_at is null"
        ),
        "insert_reset_token": (
            "insert into password_reset_tokens "  # nosec B105 - SQL 文本，非口令
            "(tenant_id, user_id, token_hash, expires_at, request_ip_hash) "
            "values (:tid, :uid, :hash, :expires_at, :ip_hash)"
        ),
        "select_reset_by_hash": (
            "select id, user_id, expires_at, used_at from password_reset_tokens "
            "where tenant_id = :tid and token_hash = :hash"
        ),
        "consume_reset_token": (
            "update password_reset_tokens set used_at = now() "  # nosec B105 - SQL 文本，非口令
            "where tenant_id = :tid and id = :id and used_at is null returning id"
        ),
        "select_mfa_state": (
            "select status, token_version, must_change_password, mfa_enabled, mfa_secret_enc, "
            "mfa_last_used_step, failed_login_attempts, failed_login_window_started_at, "
            "locked_until, login_id from users where tenant_id = :tid and id = :uid"
        ),
        "select_mfa_enabled": (
            "select mfa_enabled from users where tenant_id = :tid and id = :uid"
        ),
        "enable_mfa": (
            "update users set mfa_enabled = true, mfa_secret_enc = :enc, "
            "mfa_last_used_step = :step, failed_login_attempts = 0, "
            "failed_login_window_started_at = null, locked_until = null, "
            "token_version = token_version + 1, updated_at = now() "
            "where tenant_id = :tid and id = :uid returning token_version"
        ),
        "disable_mfa": (
            "update users set mfa_enabled = false, mfa_secret_enc = null, "
            "mfa_last_used_step = null, failed_login_attempts = 0, "
            "failed_login_window_started_at = null, locked_until = null, "
            "token_version = token_version + 1, updated_at = now() "
            "where tenant_id = :tid and id = :uid returning token_version"
        ),
        "insert_recovery_code": (
            "insert into mfa_recovery_codes (tenant_id, user_id, code_hash) "
            "values (:tid, :uid, :hash)"
        ),
        "delete_recovery_codes": (
            "delete from mfa_recovery_codes where tenant_id = :tid and user_id = :uid"
        ),
        "update_mfa_step": (
            "update users set mfa_last_used_step = :step, updated_at = now() "
            "where tenant_id = :tid and id = :uid "
            "and (mfa_last_used_step is null or mfa_last_used_step < :step) returning id"
        ),
        "select_recovery_code": (
            "select id from mfa_recovery_codes "
            "where tenant_id = :tid and user_id = :uid and code_hash = :hash and used_at is null"
        ),
        "consume_recovery_code": (
            "update mfa_recovery_codes set used_at = now() "
            "where tenant_id = :tid and id = :id and used_at is null returning id"
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
            "revoked_at, revoked_reason, rotated_to_jti, device_label, mfa_verified "
            "from platform_refresh_tokens where jti = :jti"
        ),
        "family_revoked": (
            "select 1 from platform_refresh_tokens "
            "where platform_user_id = :uid and family_id = :fid and revoked_at is not null "
            "  and revoked_reason is distinct from 'rotated' limit 1"
        ),
        "select_user_state": (
            "select status, token_version, must_change_password from platform_users where id = :uid"
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
        "select_credentials": (
            "select status, password_hash, account as login_id, failed_login_attempts, "
            "failed_login_window_started_at, locked_until from platform_users where id = :uid"
        ),
        "set_credential_hash": (
            "update platform_users set password_hash = :hash, must_change_password = false, "
            "password_changed_at = now(), token_version = token_version + 1, "
            "failed_login_attempts = 0, failed_login_window_started_at = null, "
            "locked_until = null, updated_at = now() where id = :uid"
        ),
        "select_login": (
            "select id, status, password_hash, token_version, must_change_password, "
            "mfa_enabled, failed_login_attempts, failed_login_window_started_at, locked_until "
            "from platform_users where account = :login"
        ),
        "update_login_failure": (
            "update platform_users set failed_login_attempts = :attempts, "
            "failed_login_window_started_at = :window_started, "
            "locked_until = :locked_until, updated_at = now() where id = :uid"
        ),
        "clear_login_failures": (
            "update platform_users set failed_login_attempts = 0, "
            "failed_login_window_started_at = null, locked_until = null, updated_at = now() "
            "where id = :uid "
            "and (failed_login_attempts <> 0 or failed_login_window_started_at is not null "
            "     or locked_until is not null)"
        ),
        "select_password_history": (
            "select password_hash from platform_password_history "  # nosec B105 - SQL 文本，非口令
            "where platform_user_id = :uid "
            "order by created_at desc, id desc limit :limit"
        ),
        "insert_password_history": (
            "insert into platform_password_history (platform_user_id, password_hash) "  # nosec B105
            "values (:uid, :hash)"
        ),
        "trim_password_history": (
            "delete from platform_password_history "  # nosec B105 - SQL 文本，非口令
            "where platform_user_id = :uid and id not in ("
            "  select id from platform_password_history "
            "  where platform_user_id = :uid "
            "  order by created_at desc, id desc limit :keep"
            ")"
        ),
        "revoke_user_tokens": (
            "update platform_refresh_tokens set revoked_at = now(), "
            "revoked_reason = :reason, updated_at = now() "
            "where platform_user_id = :uid and revoked_at is null"
        ),
        "select_credential_version": "select token_version from platform_users where id = :uid",
        "active_families": (
            # 平台 realm：同样只数还活着的家族（TD-231）
            "select distinct family_id from platform_refresh_tokens "
            "where platform_user_id = :uid and revoked_at is null and expires_at > now()"
        ),
        "select_reset_user_by_login": (
            "select id, status, email from platform_users where account = :login"
        ),
        "latest_reset_created_at": (
            "select created_at from platform_password_reset_tokens "
            "where platform_user_id = :uid "
            "order by created_at desc, id desc limit 1"
        ),
        "invalidate_reset_tokens": (
            "update platform_password_reset_tokens set used_at = now() "  # nosec B105 - SQL 文本，非口令
            "where platform_user_id = :uid and used_at is null"
        ),
        "insert_reset_token": (
            "insert into platform_password_reset_tokens "  # nosec B105 - SQL 文本，非口令
            "(platform_user_id, token_hash, expires_at, request_ip_hash) "
            "values (:uid, :hash, :expires_at, :ip_hash)"
        ),
        "select_reset_by_hash": (
            "select id, platform_user_id as user_id, expires_at, used_at "
            "from platform_password_reset_tokens where token_hash = :hash"
        ),
        "consume_reset_token": (
            "update platform_password_reset_tokens set used_at = now() "  # nosec B105 - SQL 文本
            "where id = :id and used_at is null returning id"
        ),
        "select_mfa_state": (
            "select status, token_version, must_change_password, mfa_enabled, mfa_secret_enc, "
            "mfa_last_used_step, failed_login_attempts, failed_login_window_started_at, "
            "locked_until, account as login_id from platform_users where id = :uid"
        ),
        "select_mfa_enabled": "select mfa_enabled from platform_users where id = :uid",
        "enable_mfa": (
            "update platform_users set mfa_enabled = true, mfa_secret_enc = :enc, "
            "mfa_last_used_step = :step, failed_login_attempts = 0, "
            "failed_login_window_started_at = null, locked_until = null, "
            "token_version = token_version + 1, updated_at = now() "
            "where id = :uid returning token_version"
        ),
        "disable_mfa": (
            "update platform_users set mfa_enabled = false, mfa_secret_enc = null, "
            "mfa_last_used_step = null, failed_login_attempts = 0, "
            "failed_login_window_started_at = null, locked_until = null, "
            "token_version = token_version + 1, updated_at = now() "
            "where id = :uid returning token_version"
        ),
        "insert_recovery_code": (
            "insert into platform_mfa_recovery_codes (platform_user_id, code_hash) "
            "values (:uid, :hash)"
        ),
        "delete_recovery_codes": (
            "delete from platform_mfa_recovery_codes where platform_user_id = :uid"
        ),
        "update_mfa_step": (
            "update platform_users set mfa_last_used_step = :step, updated_at = now() "
            "where id = :uid "
            "and (mfa_last_used_step is null or mfa_last_used_step < :step) returning id"
        ),
        "select_recovery_code": (
            "select id from platform_mfa_recovery_codes "
            "where platform_user_id = :uid and code_hash = :hash and used_at is null"
        ),
        "consume_recovery_code": (
            "update platform_mfa_recovery_codes set used_at = now() "
            "where id = :id and used_at is null returning id"
        ),
    },
}


def _refresh_insert_sql(realm: Realm) -> str:
    """刷新令牌插入语句（租户/平台各一条字面量）。"""

    if realm == "tenant":
        return (
            "insert into refresh_tokens "
            "(id, tenant_id, user_id, jti, family_id, token_hash, expires_at, "
            "last_seen_at, device_label, ua_hash, ip_hash, mfa_verified) "
            "values (gen_random_uuid(), :tenant_id, :user_id, :jti, :family_id, "
            ":token_hash, :expires_at, :last_seen_at, :device_label, :ua_hash, :ip_hash, "
            ":mfa_verified)"
        )
    return (
        "insert into platform_refresh_tokens "
        "(id, platform_user_id, jti, family_id, token_hash, expires_at, "
        "last_seen_at, device_label, ua_hash, ip_hash, mfa_verified) "
        "values (gen_random_uuid(), :user_id, :jti, :family_id, "
        ":token_hash, :expires_at, :last_seen_at, :device_label, :ua_hash, :ip_hash, "
        ":mfa_verified)"
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
    # 本会话是否完成过 MFA（写入刷新行并体现在访问令牌 amr；改密重签时保留）
    mfa_verified: bool = False


@dataclass(frozen=True, slots=True)
class MfaChallenge:
    """密码正确、已绑定 MFA：返回短时挑战令牌（不签发会话、不设 Cookie）。"""

    mfa_token: str
    expires_in: int
    realm: Realm


@dataclass(frozen=True, slots=True)
class MfaEnrollmentRequired:
    """平台强制 MFA 且账号未绑定：返回受限绑定令牌（不签发会话、不设 Cookie）。"""

    enroll_token: str
    expires_in: int
    realm: Realm


LoginOutcome = TokenPair | MfaChallenge | MfaEnrollmentRequired


@dataclass(frozen=True, slots=True)
class MfaActor:
    """MFA 绑定/激活的操作主体。

    来源有两种：普通访问令牌（已登录用户）或受限 `mfa_enroll` 令牌
    （平台强制绑定但尚未签发会话）。`token_version` 仅对后者有意义，
    用于识别改密/停用后过期的绑定令牌。
    """

    user_id: UUID
    realm: Realm
    tenant_id: UUID | None
    token_version: int | None = None
    from_enroll_token: bool = False


@dataclass(frozen=True, slots=True)
class MfaEnrollment:
    """一次绑定的待验证信息（secret/URI 只在响应中出现一次）。"""

    secret: str
    otpauth_uri: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class MfaActivation:
    """激活结果：新会话 + 一次性恢复码明文。"""

    pair: TokenPair
    recovery_codes: list[str]


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


class _ResetFailure(Exception):
    """内部信号：重置确认业务失败（事务回滚后由外层写审计并映射响应）。

    `token_found` 表示失败时令牌行确实存在于该租户（用于区分真实失败与伪造 tenant_id，
    后者只写平台审计，避免往任意租户的审计日志里灌数据）。
    """

    def __init__(
        self, reason: str, *, message: str | None = None, token_found: bool = False
    ) -> None:
        """初始化失败信号。"""

        super().__init__(reason)
        self.reason = reason
        self.message = message
        self.token_found = token_found


@dataclass(frozen=True, slots=True)
class _PreparedReset:
    """令牌已提交、待发送的重置邮件（明文令牌只在内存中短暂存在）。"""

    email: str
    token: str


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


def _mfa_invalid_challenge() -> AuthError:
    """MFA 挑战/绑定令牌失效的统一错误（不泄露账号状态）。"""

    return AuthError("MFA_LOGIN_EXPIRED", MFA_INVALID_MESSAGE, 401)


def _mfa_unavailable() -> AuthError:
    """MFA 依赖的 Redis 状态不可用：fail-closed，不签发会话也不改变账号状态。"""

    return AuthError("MFA_UNAVAILABLE", "多因素认证暂不可用，请稍后重试", 503)


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
    mfa_verified: bool = False,
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
        amr=("pwd", "totp") if mfa_verified else ("pwd",),
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
        "mfa_verified": mfa_verified,
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
        mfa_verified=mfa_verified,
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


def _weak_password(message: str) -> AuthError:
    """统一的新密码不合规错误（422）。"""

    return AuthError("WEAK_PASSWORD", message, 422)


async def is_mfa_enabled(*, realm: Realm, tenant_id: UUID | None, user_id: UUID) -> bool:
    """读取账号当前是否已启用 MFA（`/auth/me` 展示用）。"""

    async with session_store.session_scope(realm=realm, tenant_id=tenant_id) as session:
        value = await session.scalar(
            text(_SQL[realm]["select_mfa_enabled"]),
            {"uid": user_id, **_scope_params(realm, tenant_id)},
        )
    return bool(value)


def _validate_new_password(new_password: str, *, login_id: str) -> None:
    """校验密码复杂度；策略错误映射为 422（不泄露内部规则细节之外的信息）。"""

    try:
        validate_password_strength(new_password, login_id=login_id)
    except PasswordPolicyError as exc:
        raise _weak_password(str(exc)) from exc


async def _recent_password_hashes(
    session: AsyncSession,
    *,
    realm: Realm,
    tenant_id: UUID | None,
    user_id: UUID,
    limit: int,
) -> list[str]:
    """读取最近 N 条历史密码哈希（不含当前密码）。"""

    params: dict[str, object] = {"uid": user_id, "limit": limit}
    params.update(_scope_params(realm, tenant_id))
    values: list[object] = list(
        (await session.execute(text(_SQL[realm]["select_password_history"]), params))
        .scalars()
        .all()
    )
    return [str(value) for value in values]


async def _ensure_password_not_reused(
    session: AsyncSession,
    *,
    realm: Realm,
    tenant_id: UUID | None,
    user_id: UUID,
    new_password: str,
    current_hash: str,
    history_size: int,
) -> None:
    """拒绝复用当前密码或最近 N 个旧密码。"""

    hashes = [
        current_hash,
        *await _recent_password_hashes(
            session, realm=realm, tenant_id=tenant_id, user_id=user_id, limit=history_size
        ),
    ]
    reused = await anyio.to_thread.run_sync(is_password_reused, new_password, hashes)
    if reused:
        raise _weak_password("新密码不能与当前密码或最近使用过的密码相同")


async def _record_password_history(
    session: AsyncSession,
    *,
    realm: Realm,
    tenant_id: UUID | None,
    user_id: UUID,
    old_hash: str,
    keep: int,
) -> None:
    """把旧密码写入历史并裁剪到最近 N 条。"""

    params: dict[str, object] = {"uid": user_id, "hash": old_hash}
    params.update(_scope_params(realm, tenant_id))
    await session.execute(text(_SQL[realm]["insert_password_history"]), params)
    await session.execute(
        text(_SQL[realm]["trim_password_history"]),
        {"uid": user_id, "keep": keep, **_scope_params(realm, tenant_id)},
    )


async def _clear_login_failures(
    session: AsyncSession, *, realm: Realm, tenant_id: UUID | None, user_id: UUID
) -> None:
    """成功登录后清零失败窗口与锁定（仅在有数据时更新）。"""

    await session.execute(
        text(_SQL[realm]["clear_login_failures"]),
        {"uid": user_id, **_scope_params(realm, tenant_id)},
    )


async def _apply_login_failure(
    session: AsyncSession,
    *,
    realm: Realm,
    tenant_id: UUID | None,
    user_id: UUID,
    state: LockoutState,
    settings: Settings,
    now: datetime,
) -> LockoutState:
    """在同一事务内累加失败次数并按阈值锁定（调用方持有用户 advisory lock）。"""

    updated = register_login_failure(
        state,
        now=now,
        max_failures=settings.auth_max_login_failures,
        window_seconds=settings.auth_login_failure_window_seconds,
        lockout_seconds=settings.auth_lockout_seconds,
    )
    await session.execute(
        text(_SQL[realm]["update_login_failure"]),
        {
            "uid": user_id,
            "attempts": updated.failed_attempts,
            "window_started": updated.window_started_at,
            "locked_until": updated.locked_until,
            **_scope_params(realm, tenant_id),
        },
    )
    return updated


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
) -> LoginOutcome:
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
            (
                await platform_session.execute(
                    text("select id, status from tenants where school_code = :code"),
                    {"code": code},
                )
            )
            .mappings()
            .one_or_none()
        )

    if tenant_row is None or tenant_row["status"] != "active":
        record_login_failure("tenant", "tenant_unavailable")
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
    outcome: LoginOutcome | None = None
    kicked: list[UUID] = []
    failure_reason = "invalid_password"
    failure_state: LockoutState | None = None

    async with db_session(tenant_id=tenant_id) as session:
        # 先取 id 再加锁，锁后重读：避免并发改密/停用后签发旧 token_version
        user_id_row = (
            await session.execute(
                text("select id from users where tenant_id = :tid and login_id = :login"),
                {"tid": tenant_id, "login": login},
            )
        ).first()
        if user_id_row is not None:
            await session_store.acquire_user_lock(session, realm="tenant", user_id=user_id_row[0])

        now = datetime.now(UTC)
        user_row = (
            (
                await session.execute(
                    text(_SQL["tenant"]["select_login"]),
                    {"tid": tenant_id, "login": login},
                )
            )
            .mappings()
            .one_or_none()
        )

        locked = user_row is not None and is_account_locked(
            cast(datetime | None, user_row["locked_until"]), now
        )
        if user_row is None:
            failure_reason = "unknown_account"
        elif user_row["status"] != "active":
            failure_reason = "account_unavailable"
        elif locked:
            failure_reason = "locked"

        stored_hash = (
            str(user_row["password_hash"]) if user_row is not None else _TIMING_EQUALIZATION_HASH
        )
        password_ok = False
        if len(password.encode("utf-8")) <= MAX_PASSWORD_BYTES:
            # 账号不存在/锁定时同样比对：失败路径耗时一致（反账号枚举）
            password_ok = await anyio.to_thread.run_sync(verify_password, password, stored_hash)

        roles: frozenset[str] = frozenset()
        if user_row is not None and user_row["status"] == "active":
            roles = await _load_roles(
                session, realm="tenant", tenant_id=tenant_id, user_id=user_row["id"]
            )

        if user_row is not None and password_ok and user_row["status"] == "active" and not locked:
            if bool(user_row["mfa_enabled"]):
                # 密码通过但还需 TOTP：不清失败计数、不占会话名额、不签发会话
                mfa_token, challenge_jti = create_mfa_token(
                    settings,
                    purpose="mfa_challenge",
                    user_id=user_row["id"],
                    realm="tenant",
                    tenant_id=tenant_id,
                    token_version=int(user_row["token_version"]),
                )
                try:
                    await revocation.register_mfa_challenge(
                        str(challenge_jti),
                        realm="tenant",
                        ttl_seconds=settings.mfa_challenge_ttl_seconds,
                    )
                except revocation.RedisUnavailableError as exc:
                    raise _mfa_unavailable() from exc
                outcome = MfaChallenge(
                    mfa_token=mfa_token,
                    expires_in=settings.mfa_challenge_ttl_seconds,
                    realm="tenant",
                )
            else:
                if (
                    int(user_row["failed_login_attempts"])
                    or user_row["failed_login_window_started_at"] is not None
                    or user_row["locked_until"] is not None
                ):
                    await _clear_login_failures(
                        session, realm="tenant", tenant_id=tenant_id, user_id=user_row["id"]
                    )
                kicked = await session_store.enforce_limit(
                    session,
                    realm="tenant",
                    tenant_id=tenant_id,
                    user_id=user_row["id"],
                    limit=settings.max_sessions_per_user_tenant,
                    request_id=request_id,
                )
                outcome = await _issue_tokens(
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
                        "mfa": False,
                    },
                    request_id=request_id,
                )
                # 标记在事务内登记：撤销方串行化后不会再被"提交后补写"复活
                await revocation.register_session(outcome.session_id, realm="tenant")
        elif failure_reason == "invalid_password" and user_row is not None:
            failure_state = await _apply_login_failure(
                session,
                realm="tenant",
                tenant_id=tenant_id,
                user_id=user_row["id"],
                state=LockoutState(
                    failed_attempts=int(user_row["failed_login_attempts"]),
                    window_started_at=cast(
                        datetime | None, user_row["failed_login_window_started_at"]
                    ),
                    locked_until=cast(datetime | None, user_row["locked_until"]),
                ),
                settings=settings,
                now=now,
            )

    if outcome is None:
        record_login_failure(
            "tenant",
            failure_reason,
            lock_applied=failure_state is not None and failure_state.locked_until is not None,
        )
        async with db_session(tenant_id=tenant_id) as audit_session:
            metadata: dict[str, object] = {
                "login_masked": login[:1] + "***",
                "reason": failure_reason,
                "ip_hash": ip_hash,
                "ua_hash": ua_hash,
            }
            if failure_state is not None:
                metadata["failed_attempts"] = failure_state.failed_attempts
                metadata["lock_applied"] = failure_state.locked_until is not None
            await append_audit(
                audit_session,
                action="auth.login.failed",
                resource_type="auth",
                actor_realm="tenant",
                tenant_id=tenant_id,
                result="failure",
                metadata=metadata,
                request_id=request_id,
            )
        raise _invalid_credentials()

    if isinstance(outcome, (MfaChallenge, MfaEnrollmentRequired)):
        return outcome

    # 事务已提交（新标记已在事务内登记）：清理被踢设备的标记；失败只告警，
    # 这些 family 已在数据库撤销，刷新令牌无法再用。
    await revocation.delete_sessions(kicked, realm="tenant")
    return outcome


async def login_platform(
    *,
    account: str,
    password: str,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> LoginOutcome:
    """平台账号登录。

    异常:
        AuthError: 凭证错误（401，文案统一）。
    """

    settings = get_settings()
    normalized = account.strip()
    ip_hash = hash_client_fingerprint(ip)
    ua_hash = hash_client_fingerprint(user_agent)
    device_label = session_store.device_label_from_user_agent(user_agent)
    outcome: LoginOutcome | None = None
    kicked: list[UUID] = []
    failure_reason = "invalid_password"
    failure_state: LockoutState | None = None

    async with db_session(platform=True) as session:
        user_id_row = (
            await session.execute(
                text("select id from platform_users where account = :account"),
                {"account": normalized},
            )
        ).first()
        if user_id_row is not None:
            await session_store.acquire_user_lock(session, realm="platform", user_id=user_id_row[0])

        now = datetime.now(UTC)
        row = (
            (await session.execute(text(_SQL["platform"]["select_login"]), {"login": normalized}))
            .mappings()
            .one_or_none()
        )

        locked = row is not None and is_account_locked(
            cast(datetime | None, row["locked_until"]), now
        )
        if row is None:
            failure_reason = "unknown_account"
        elif row["status"] != "active":
            failure_reason = "account_unavailable"
        elif locked:
            failure_reason = "locked"

        stored_hash = str(row["password_hash"]) if row is not None else _TIMING_EQUALIZATION_HASH
        password_ok = False
        if len(password.encode("utf-8")) <= MAX_PASSWORD_BYTES:
            password_ok = await anyio.to_thread.run_sync(verify_password, password, stored_hash)

        roles: frozenset[str] = frozenset()
        if row is not None and row["status"] == "active":
            roles = await _load_roles(session, realm="platform", tenant_id=None, user_id=row["id"])

        if row is not None and password_ok and row["status"] == "active" and not locked:
            mfa_required = bool(row["mfa_enabled"]) or settings.mfa_require_platform
            if mfa_required:
                # 密码通过但需 MFA/绑定：不签会话、不清失败计数、不占会话名额
                if bool(row["mfa_enabled"]):
                    mfa_token, challenge_jti = create_mfa_token(
                        settings,
                        purpose="mfa_challenge",
                        user_id=row["id"],
                        realm="platform",
                        tenant_id=None,
                        token_version=int(row["token_version"]),
                    )
                    try:
                        await revocation.register_mfa_challenge(
                            str(challenge_jti),
                            realm="platform",
                            ttl_seconds=settings.mfa_challenge_ttl_seconds,
                        )
                    except revocation.RedisUnavailableError as exc:
                        raise _mfa_unavailable() from exc
                    outcome = MfaChallenge(
                        mfa_token=mfa_token,
                        expires_in=settings.mfa_challenge_ttl_seconds,
                        realm="platform",
                    )
                else:
                    enroll_token, _ = create_mfa_token(
                        settings,
                        purpose="mfa_enroll",
                        user_id=row["id"],
                        realm="platform",
                        tenant_id=None,
                        token_version=int(row["token_version"]),
                    )
                    outcome = MfaEnrollmentRequired(
                        enroll_token=enroll_token,
                        expires_in=settings.mfa_enroll_token_ttl_seconds,
                        realm="platform",
                    )
            else:
                if (
                    int(row["failed_login_attempts"])
                    or row["failed_login_window_started_at"] is not None
                    or row["locked_until"] is not None
                ):
                    await _clear_login_failures(
                        session, realm="platform", tenant_id=None, user_id=row["id"]
                    )
                kicked = await session_store.enforce_limit(
                    session,
                    realm="platform",
                    tenant_id=None,
                    user_id=row["id"],
                    limit=settings.max_sessions_per_user_platform,
                    request_id=request_id,
                )
                outcome = await _issue_tokens(
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
                    metadata={
                        "realm": "platform",
                        "ip_hash": ip_hash,
                        "ua_hash": ua_hash,
                        "mfa": False,
                    },
                    request_id=request_id,
                )
                await revocation.register_session(outcome.session_id, realm="platform")
        elif failure_reason == "invalid_password" and row is not None:
            failure_state = await _apply_login_failure(
                session,
                realm="platform",
                tenant_id=None,
                user_id=row["id"],
                state=LockoutState(
                    failed_attempts=int(row["failed_login_attempts"]),
                    window_started_at=cast(datetime | None, row["failed_login_window_started_at"]),
                    locked_until=cast(datetime | None, row["locked_until"]),
                ),
                settings=settings,
                now=now,
            )

    if outcome is None:
        record_login_failure(
            "platform",
            failure_reason,
            lock_applied=failure_state is not None and failure_state.locked_until is not None,
        )
        async with db_session(platform=True) as audit_session:
            metadata: dict[str, object] = {
                "realm": "platform",
                "account_masked": normalized[:1] + "***",
                "reason": failure_reason,
                "ip_hash": ip_hash,
                "ua_hash": ua_hash,
            }
            if failure_state is not None:
                metadata["failed_attempts"] = failure_state.failed_attempts
                metadata["lock_applied"] = failure_state.locked_until is not None
            await append_audit(
                audit_session,
                action="auth.login.failed",
                resource_type="auth",
                actor_realm="platform",
                result="failure",
                metadata=metadata,
                request_id=request_id,
            )
        raise _invalid_credentials()

    if isinstance(outcome, (MfaChallenge, MfaEnrollmentRequired)):
        return outcome

    await revocation.delete_sessions(kicked, realm="platform")
    return outcome


async def _consume_recovery_code(
    session: AsyncSession,
    *,
    realm: Realm,
    tenant_id: UUID | None,
    user_id: UUID,
    code: str,
    subkey: bytes,
) -> bool:
    """匹配并消费一次性恢复码（条件更新保证并发下只成功一次）。"""

    try:
        digest = hash_recovery_code(code, subkey=subkey, realm=realm, user_id=user_id)
    except ValueError:
        # 格式不是恢复码（如 6 位 TOTP）：按不匹配处理
        return False
    params: dict[str, object] = {"uid": user_id, "hash": digest}
    params.update(_scope_params(realm, tenant_id))
    row = (await session.execute(text(_SQL[realm]["select_recovery_code"]), params)).first()
    if row is None:
        return False
    consumed = (
        await session.execute(
            text(_SQL[realm]["consume_recovery_code"]),
            {"id": row[0], **_scope_params(realm, tenant_id)},
        )
    ).first()
    return consumed is not None


async def verify_mfa(
    *,
    mfa_token: str,
    code: str,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> TokenPair:
    """完成登录第二步（TOTP 或恢复码），成功后签发 `mfa_verified=true` 的会话。

    校验顺序：挑战令牌 → Redis 尝试次数 → 用户锁 → 重读账号状态 → 解密 secret
    → TOTP（含重放水位）或恢复码。验证码错误计入 AUTH-05 锁定；MFA 解密失败
    fail-closed（500），不签发会话。

    异常:
        AuthError: 挑战失效/已用（401）、验证码错误/锁定（401，与密码错误同文案）、
            账号不可用（401）、MFA 密钥损坏（500）。
    """

    settings = get_settings()
    try:
        claims = decode_mfa_token(settings, mfa_token, expected_purpose="mfa_challenge")
    except TokenError as exc:
        raise _mfa_invalid_challenge() from exc

    try:
        attempt = await revocation.begin_mfa_challenge_attempt(
            str(claims.jti),
            realm=claims.realm,
            ttl_seconds=settings.mfa_challenge_ttl_seconds,
            max_attempts=settings.mfa_challenge_max_attempts,
        )
    except revocation.RedisUnavailableError as exc:
        raise _mfa_unavailable() from exc
    if attempt is None:
        raise _mfa_invalid_challenge()

    realm = claims.realm
    tenant_id = claims.tenant_id
    ip_hash = hash_client_fingerprint(ip)
    ua_hash = hash_client_fingerprint(user_agent)
    device_label = session_store.device_label_from_user_agent(user_agent)

    pair: TokenPair | None = None
    kicked: list[UUID] = []
    failure_reason: str | None = None
    failure_state: LockoutState | None = None
    recovery_used = False
    secret_invalid = False

    async with session_store.session_scope(realm=realm, tenant_id=tenant_id) as session:
        await session_store.acquire_user_lock(session, realm=realm, user_id=claims.subject)
        row = (
            (
                await session.execute(
                    text(_SQL[realm]["select_mfa_state"]),
                    {"uid": claims.subject, **_scope_params(realm, tenant_id)},
                )
            )
            .mappings()
            .one_or_none()
        )
        now = datetime.now(UTC)
        state: RowMapping | None = row
        if state is None:
            failure_reason = "unknown_account"
        elif state["status"] != "active":
            failure_reason = "account_unavailable"
        elif int(state["token_version"]) != claims.token_version:
            # 改密/停用/MFA 变更已轮换会话：旧挑战必须作废，否则可绕过新状态
            failure_reason = "stale_challenge"
        elif is_account_locked(cast(datetime | None, state["locked_until"]), now):
            failure_reason = "locked"
        elif not bool(state["mfa_enabled"]) or not state["mfa_secret_enc"]:
            failure_reason = "mfa_disabled"

        if state is not None and failure_reason is None:
            try:
                kek = decode_kek(settings.encryption_kek)
                secret = decrypt_secret(
                    str(state["mfa_secret_enc"]),
                    kek=kek,
                    aad=mfa_aad(realm, claims.subject),
                )
            except (SecretDecryptError, SecretEncryptionError):
                # 密钥/数据损坏：fail-closed，审计后 500（不返回 secret）
                logger.error("MFA secret 解密失败（realm=%s）", realm)
                secret_invalid = True
            else:
                step = verify_totp(
                    secret,
                    code,
                    interval=settings.mfa_totp_interval_seconds,
                    window_steps=settings.mfa_totp_window_steps,
                    last_used_step=cast(int | None, state["mfa_last_used_step"]),
                )
                if step is not None:
                    updated = (
                        await session.execute(
                            text(_SQL[realm]["update_mfa_step"]),
                            {
                                "step": step,
                                "uid": claims.subject,
                                **_scope_params(realm, tenant_id),
                            },
                        )
                    ).first()
                    if updated is None:
                        # 并发校验已消费该时间步：按重放拒绝
                        step = None
                if step is None:
                    recovery_used = await _consume_recovery_code(
                        session,
                        realm=realm,
                        tenant_id=tenant_id,
                        user_id=claims.subject,
                        code=code,
                        subkey=recovery_code_subkey(kek),
                    )
                if step is None and not recovery_used:
                    failure_reason = "invalid_mfa_code"
                else:
                    # 凭证通过。GETDEL 是"单次投递"闸门：放在签发会话之前。
                    # 上面的水位推进/恢复码消费与本事务同生共死——若挑战已被并发消费
                    # （challenge_live=False）则抛错回滚，不会留下烧掉的码或前进的水位。
                    try:
                        challenge_live = await revocation.consume_mfa_challenge(
                            str(claims.jti), realm=realm
                        )
                    except revocation.RedisUnavailableError as exc:
                        raise _mfa_unavailable() from exc
                    if not challenge_live:
                        # 挑战已不存在（并发/过期）：抛错回滚，不烧恢复码也不推进时间步水位
                        raise _mfa_invalid_challenge()

        if state is not None and failure_reason is None and not secret_invalid:
            if (
                int(state["failed_login_attempts"])
                or state["failed_login_window_started_at"] is not None
                or state["locked_until"] is not None
            ):
                await _clear_login_failures(
                    session, realm=realm, tenant_id=tenant_id, user_id=claims.subject
                )
            kicked = await session_store.enforce_limit(
                session,
                realm=realm,
                tenant_id=tenant_id,
                user_id=claims.subject,
                limit=(
                    settings.max_sessions_per_user_tenant
                    if realm == "tenant"
                    else settings.max_sessions_per_user_platform
                ),
                request_id=request_id,
            )
            roles = await _load_roles(
                session, realm=realm, tenant_id=tenant_id, user_id=claims.subject
            )
            pair = await _issue_tokens(
                settings,
                realm=realm,
                user_id=claims.subject,
                tenant_id=tenant_id,
                token_version=int(state["token_version"]),
                must_change_password=bool(state["must_change_password"]),
                ip_hash=ip_hash,
                ua_hash=ua_hash,
                session=session,
                device_label=device_label,
                roles=roles,
                mfa_verified=True,
            )
            await append_audit(
                session,
                action="auth.login.success",
                resource_type="auth",
                actor_realm=realm,
                actor_id=claims.subject,
                tenant_id=tenant_id,
                metadata={
                    "realm": realm,
                    "mfa": True,
                    "recovery_code_used": recovery_used,
                    "ip_hash": ip_hash,
                    "ua_hash": ua_hash,
                },
                request_id=request_id,
            )
            if recovery_used:
                await append_audit(
                    session,
                    action="auth.mfa.recovery_used",
                    resource_type="auth",
                    actor_realm=realm,
                    actor_id=claims.subject,
                    tenant_id=tenant_id,
                    metadata={"phase": "login", "ip_hash": ip_hash},
                    request_id=request_id,
                )
            await revocation.register_session(pair.session_id, realm=realm)
        elif state is not None and failure_reason == "invalid_mfa_code":
            failure_state = await _apply_login_failure(
                session,
                realm=realm,
                tenant_id=tenant_id,
                user_id=claims.subject,
                state=LockoutState(
                    failed_attempts=int(state["failed_login_attempts"]),
                    window_started_at=cast(
                        datetime | None, state["failed_login_window_started_at"]
                    ),
                    locked_until=cast(datetime | None, state["locked_until"]),
                ),
                settings=settings,
                now=now,
            )

    if secret_invalid:
        await _audit_mfa_secret_invalid(
            realm=realm, tenant_id=tenant_id, user_id=claims.subject, request_id=request_id
        )
        raise AuthError("MFA_UNAVAILABLE", "多因素认证暂不可用，请稍后重试或联系管理员", 500)

    if failure_reason is not None:
        await _audit_mfa_login_failure(
            realm=realm,
            tenant_id=tenant_id,
            user_id=claims.subject,
            reason=failure_reason,
            state=failure_state,
            request_id=request_id,
            ip_hash=ip_hash,
            ua_hash=ua_hash,
        )
        if failure_reason in {"invalid_mfa_code", "locked"}:
            raise _invalid_credentials()
        raise _mfa_invalid_challenge()

    if pair is None:
        raise RuntimeError("verify_mfa 未返回令牌")

    await revocation.delete_sessions(kicked, realm=realm)
    return pair


async def _audit_mfa_login_failure(
    *,
    realm: Realm,
    tenant_id: UUID | None,
    user_id: UUID,
    reason: str,
    state: LockoutState | None,
    request_id: str | None,
    ip_hash: str | None,
    ua_hash: str | None,
) -> None:
    """MFA 登录失败审计（独立事务，避免随验证事务回滚丢失）。"""

    record_login_failure(
        realm,
        reason,
        lock_applied=state is not None and state.locked_until is not None,
    )
    metadata: dict[str, object] = {
        "realm": realm,
        "mfa": True,
        "reason": reason,
        "ip_hash": ip_hash,
        "ua_hash": ua_hash,
    }
    if state is not None:
        metadata["failed_attempts"] = state.failed_attempts
        metadata["lock_applied"] = state.locked_until is not None

    if realm == "tenant" and tenant_id is not None:
        if await _tenant_exists(tenant_id):
            async with db_session(tenant_id=tenant_id) as session:
                await append_audit(
                    session,
                    action="auth.login.failed",
                    resource_type="auth",
                    actor_realm="tenant",
                    actor_id=user_id,
                    tenant_id=tenant_id,
                    result="failure",
                    metadata=metadata,
                    request_id=request_id,
                )
            return
        metadata["attempted_tenant_id"] = str(tenant_id)

    async with db_session(platform=True) as session:
        await append_audit(
            session,
            action="auth.login.failed",
            resource_type="auth",
            actor_realm="platform" if realm == "platform" else "system",
            actor_id=user_id if realm == "platform" else None,
            result="failure",
            metadata=metadata,
            request_id=request_id,
        )


async def _audit_mfa_secret_invalid(
    *,
    realm: Realm,
    tenant_id: UUID | None,
    user_id: UUID,
    request_id: str | None,
) -> None:
    """MFA 密文解密失败审计（不含密文/密钥；租户不存在时降级为平台事件）。"""

    metadata: dict[str, object] = {"realm": realm, "reason": "decrypt_failed"}
    if realm == "tenant" and tenant_id is not None:
        if await _tenant_exists(tenant_id):
            async with db_session(tenant_id=tenant_id) as session:
                await append_audit(
                    session,
                    action="auth.mfa.secret_invalid",
                    resource_type="auth",
                    actor_realm="tenant",
                    actor_id=user_id,
                    tenant_id=tenant_id,
                    result="failure",
                    metadata=metadata,
                    request_id=request_id,
                )
            return
        metadata["attempted_tenant_id"] = str(tenant_id)

    async with db_session(platform=True) as session:
        await append_audit(
            session,
            action="auth.mfa.secret_invalid",
            resource_type="auth",
            actor_realm="platform" if realm == "platform" else "system",
            actor_id=user_id if realm == "platform" else None,
            result="failure",
            metadata=metadata,
            request_id=request_id,
        )


async def _append_realm_audit(
    *,
    action: str,
    realm: Realm,
    tenant_id: UUID | None,
    user_id: UUID | None,
    result: str,
    metadata: dict[str, object],
    request_id: str | None,
) -> None:
    """独立事务写认证审计；租户不存在时降级为平台事件（不因外键违约丢审计）。"""

    if realm == "tenant" and tenant_id is not None:
        if await _tenant_exists(tenant_id):
            async with db_session(tenant_id=tenant_id) as session:
                await append_audit(
                    session,
                    action=action,
                    resource_type="auth",
                    actor_realm="tenant",
                    actor_id=user_id,
                    tenant_id=tenant_id,
                    result=result,
                    metadata=metadata,
                    request_id=request_id,
                )
            return
        metadata = {**metadata, "attempted_tenant_id": str(tenant_id)}

    async with db_session(platform=True) as session:
        await append_audit(
            session,
            action=action,
            resource_type="auth",
            actor_realm="platform" if realm == "platform" else "system",
            actor_id=user_id if realm == "platform" else None,
            result=result,
            metadata=metadata,
            request_id=request_id,
        )


def _lockout_state(row: RowMapping) -> LockoutState:
    """从账号状态行构造锁定状态（保持既有失败窗口）。"""

    return LockoutState(
        failed_attempts=int(row["failed_login_attempts"]),
        window_started_at=cast(datetime | None, row["failed_login_window_started_at"]),
        locked_until=cast(datetime | None, row["locked_until"]),
    )


async def enroll_mfa(
    *,
    actor: MfaActor,
    current_password: str | None,
    request_id: str | None = None,
) -> MfaEnrollment:
    """开始 TOTP 绑定：生成/轮换待激活 secret（10 分钟内有效）。

    - 受限 `mfa_enroll` 令牌：密码已在登录时校验，不再要求 current_password；
    - 普通访问令牌：必须重新验证当前密码（防止会话被借用后静默绑定 MFA）。

    异常:
        AuthError: 密码错误/锁定（401）、需要密码（400）、已启用（409）、
            缓存不可用（503）、账号不可用（401）。
    """

    settings = get_settings()
    realm = actor.realm
    tenant_id = actor.tenant_id
    failure_reason: str | None = None
    failure_state: LockoutState | None = None
    enrollment: MfaEnrollment | None = None

    async with session_store.session_scope(realm=realm, tenant_id=tenant_id) as session:
        await session_store.acquire_user_lock(session, realm=realm, user_id=actor.user_id)
        row = (
            (
                await session.execute(
                    text(_SQL[realm]["select_mfa_state"]),
                    {"uid": actor.user_id, **_scope_params(realm, tenant_id)},
                )
            )
            .mappings()
            .one_or_none()
        )
        now = datetime.now(UTC)
        if row is None or row["status"] != "active":
            failure_reason = "account_unavailable"
        elif actor.token_version is not None and int(row["token_version"]) != actor.token_version:
            failure_reason = "stale_token"
        elif is_account_locked(cast(datetime | None, row["locked_until"]), now):
            failure_reason = "locked"
        elif bool(row["mfa_enabled"]):
            failure_reason = "already_enabled"
        elif not actor.from_enroll_token:
            if not current_password:
                failure_reason = "password_required"
            else:
                credentials = (
                    (
                        await session.execute(
                            text(_SQL[realm]["select_credentials"]),
                            {"uid": actor.user_id, **_scope_params(realm, tenant_id)},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                password_ok = credentials is not None and await anyio.to_thread.run_sync(
                    verify_password, current_password, str(credentials["password_hash"])
                )
                if not password_ok:
                    failure_state = await _apply_login_failure(
                        session,
                        realm=realm,
                        tenant_id=tenant_id,
                        user_id=actor.user_id,
                        state=_lockout_state(row),
                        settings=settings,
                        now=now,
                    )
                    failure_reason = "password_mismatch"

        if failure_reason is None and row is not None:
            secret = generate_totp_secret()
            kek = decode_kek(settings.encryption_kek)
            ciphertext = encrypt_secret(secret, kek=kek, aad=mfa_aad(realm, actor.user_id))
            stored = await revocation.store_mfa_pending_secret(
                ciphertext,
                realm=realm,
                user_id=actor.user_id,
                ttl_seconds=settings.mfa_enroll_token_ttl_seconds,
            )
            if not stored:
                failure_reason = "cache_unavailable"
            else:
                await append_audit(
                    session,
                    action="auth.mfa.enrollment_started",
                    resource_type="auth",
                    actor_realm=realm,
                    actor_id=actor.user_id,
                    tenant_id=tenant_id,
                    metadata={"method": "totp"},
                    request_id=request_id,
                )
                enrollment = MfaEnrollment(
                    secret=secret,
                    otpauth_uri=totp_uri(
                        secret,
                        issuer=settings.mfa_totp_issuer,
                        account_name=str(row["login_id"]),
                    ),
                    expires_in=settings.mfa_enroll_token_ttl_seconds,
                )

    if failure_reason is not None:
        if failure_reason == "password_mismatch":
            await _append_realm_audit(
                action="auth.mfa.enrollment_failed",
                realm=realm,
                tenant_id=tenant_id,
                user_id=actor.user_id,
                result="failure",
                metadata={
                    "reason": failure_reason,
                    "failed_attempts": (
                        failure_state.failed_attempts if failure_state is not None else None
                    ),
                    "lock_applied": (
                        failure_state.locked_until is not None
                        if failure_state is not None
                        else False
                    ),
                },
                request_id=request_id,
            )
            raise AuthError("INVALID_CREDENTIALS", "当前密码不正确", 401)
        if failure_reason == "password_required":
            raise AuthError("PASSWORD_REQUIRED", "需要验证当前密码", 400)
        if failure_reason == "already_enabled":
            raise AuthError("MFA_ALREADY_ENABLED", "多因素认证已启用", 409)
        if failure_reason == "cache_unavailable":
            raise _mfa_unavailable()
        if failure_reason == "locked":
            raise AuthError("INVALID_CREDENTIALS", "当前密码不正确", 401)
        raise _mfa_invalid_challenge()

    if enrollment is None:
        raise RuntimeError("enroll_mfa 未返回绑定信息")
    return enrollment


async def activate_mfa(
    *,
    actor: MfaActor,
    code: str,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> MfaActivation:
    """校验待激活 secret 的 TOTP，启用 MFA、生成恢复码并轮换全部会话。

    激活成功后 `token_version+1`、撤销该用户全部刷新令牌（`mfa_change`），
    返回新令牌与一次性恢复码明文（仅此次响应可见）。

    异常:
        AuthError: 验证码错误/锁定（401）、绑定过期（400）、已启用（409）、
            密文损坏（500，fail-closed）。
    """

    settings = get_settings()
    realm = actor.realm
    tenant_id = actor.tenant_id
    ip_hash = hash_client_fingerprint(ip)
    ua_hash = hash_client_fingerprint(user_agent)
    device_label = session_store.device_label_from_user_agent(user_agent)

    pair: TokenPair | None = None
    recovery_codes: list[str] = []
    old_families: list[UUID] = []
    failure_reason: str | None = None
    failure_state: LockoutState | None = None
    secret_invalid = False

    async with session_store.session_scope(realm=realm, tenant_id=tenant_id) as session:
        await session_store.acquire_user_lock(session, realm=realm, user_id=actor.user_id)
        row = (
            (
                await session.execute(
                    text(_SQL[realm]["select_mfa_state"]),
                    {"uid": actor.user_id, **_scope_params(realm, tenant_id)},
                )
            )
            .mappings()
            .one_or_none()
        )
        now = datetime.now(UTC)
        if row is None or row["status"] != "active":
            failure_reason = "account_unavailable"
        elif actor.token_version is not None and int(row["token_version"]) != actor.token_version:
            failure_reason = "stale_token"
        elif is_account_locked(cast(datetime | None, row["locked_until"]), now):
            failure_reason = "locked"
        elif bool(row["mfa_enabled"]):
            failure_reason = "already_enabled"

        if failure_reason is None and row is not None:
            try:
                pending = await revocation.get_mfa_pending_secret(
                    realm=realm, user_id=actor.user_id
                )
            except revocation.RedisUnavailableError as exc:
                # Redis 故障不能报"绑定已过期"：用户会误重绑，且此时无法判定状态
                raise _mfa_unavailable() from exc
            if pending is None:
                failure_reason = "enroll_expired"
            else:
                try:
                    kek = decode_kek(settings.encryption_kek)
                    secret = decrypt_secret(pending, kek=kek, aad=mfa_aad(realm, actor.user_id))
                except (SecretDecryptError, SecretEncryptionError):
                    logger.error("MFA 待激活 secret 解密失败（realm=%s）", realm)
                    secret_invalid = True
                else:
                    step = verify_totp(
                        secret,
                        code,
                        interval=settings.mfa_totp_interval_seconds,
                        window_steps=settings.mfa_totp_window_steps,
                    )
                    if step is None:
                        failure_state = await _apply_login_failure(
                            session,
                            realm=realm,
                            tenant_id=tenant_id,
                            user_id=actor.user_id,
                            state=_lockout_state(row),
                            settings=settings,
                            now=now,
                        )
                        failure_reason = "invalid_mfa_code"
                    else:
                        subkey = recovery_code_subkey(kek)
                        recovery_codes, hashes = generate_recovery_codes(
                            settings.mfa_recovery_code_count,
                            subkey=subkey,
                            realm=realm,
                            user_id=actor.user_id,
                        )
                        for digest in hashes:
                            await session.execute(
                                text(_SQL[realm]["insert_recovery_code"]),
                                {
                                    "uid": actor.user_id,
                                    "hash": digest,
                                    **_scope_params(realm, tenant_id),
                                },
                            )
                        version = int(
                            await session.scalar(
                                text(_SQL[realm]["enable_mfa"]),
                                {
                                    "enc": encrypt_secret(
                                        secret, kek=kek, aad=mfa_aad(realm, actor.user_id)
                                    ),
                                    "step": step,
                                    "uid": actor.user_id,
                                    **_scope_params(realm, tenant_id),
                                },
                            )
                        )
                        old_families = list(
                            (
                                await session.execute(
                                    text(_SQL[realm]["active_families"]),
                                    {"uid": actor.user_id, **_scope_params(realm, tenant_id)},
                                )
                            )
                            .scalars()
                            .all()
                        )
                        await session.execute(
                            text(_SQL[realm]["revoke_user_tokens"]),
                            {
                                "uid": actor.user_id,
                                "reason": "mfa_change",
                                **_scope_params(realm, tenant_id),
                            },
                        )
                        roles = await _load_roles(
                            session, realm=realm, tenant_id=tenant_id, user_id=actor.user_id
                        )
                        pair = await _issue_tokens(
                            settings,
                            realm=realm,
                            user_id=actor.user_id,
                            tenant_id=tenant_id,
                            token_version=version,
                            must_change_password=bool(row["must_change_password"]),
                            ip_hash=ip_hash,
                            ua_hash=ua_hash,
                            session=session,
                            device_label=device_label,
                            roles=roles,
                            mfa_verified=True,
                        )
                        await append_audit(
                            session,
                            action="auth.mfa.activated",
                            resource_type="auth",
                            actor_realm=realm,
                            actor_id=actor.user_id,
                            tenant_id=tenant_id,
                            metadata={
                                "recovery_codes_issued": len(recovery_codes),
                                # 布尔语义（"旧会话被全断了"）用独立键；整数键 `sessions_revoked`
                                # 留给"断了几台"的条数口径（TD-231）
                                "sessions_all_revoked": True,
                                "ip_hash": ip_hash,
                            },
                            request_id=request_id,
                        )
                        await append_audit(
                            session,
                            action="auth.login.success",
                            resource_type="auth",
                            actor_realm=realm,
                            actor_id=actor.user_id,
                            tenant_id=tenant_id,
                            metadata={
                                "realm": realm,
                                "mfa": True,
                                "recovery_code_used": False,
                                "ip_hash": ip_hash,
                                "ua_hash": ua_hash,
                            },
                            request_id=request_id,
                        )
                        await revocation.register_session(pair.session_id, realm=realm)

    if secret_invalid:
        await _audit_mfa_secret_invalid(
            realm=realm, tenant_id=tenant_id, user_id=actor.user_id, request_id=request_id
        )
        raise AuthError("MFA_UNAVAILABLE", "多因素认证暂不可用，请稍后重试或联系管理员", 500)

    if failure_reason is not None:
        await _append_realm_audit(
            action="auth.mfa.activation_failed",
            realm=realm,
            tenant_id=tenant_id,
            user_id=actor.user_id,
            result="failure",
            metadata={
                "reason": failure_reason,
                "failed_attempts": (
                    failure_state.failed_attempts if failure_state is not None else None
                ),
                "lock_applied": (
                    failure_state.locked_until is not None if failure_state is not None else False
                ),
            },
            request_id=request_id,
        )
        if failure_reason in {"invalid_mfa_code", "locked"}:
            raise _invalid_credentials()
        if failure_reason == "already_enabled":
            raise AuthError("MFA_ALREADY_ENABLED", "多因素认证已启用", 409)
        if failure_reason == "enroll_expired":
            raise AuthError("MFA_ENROLL_EXPIRED", "绑定已过期，请重新开始", 400)
        raise _mfa_invalid_challenge()

    if pair is None:
        raise RuntimeError("activate_mfa 未返回令牌")

    await revocation.delete_mfa_pending_secret(realm=realm, user_id=actor.user_id)
    await revocation.delete_sessions(old_families, realm=realm)
    return MfaActivation(pair=pair, recovery_codes=recovery_codes)


async def disable_mfa(
    *,
    principal_user_id: UUID,
    realm: Realm,
    tenant_id: UUID | None,
    current_password: str,
    code: str,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> TokenPair:
    """关闭 MFA：校验密码 + TOTP/恢复码，清 secret 与恢复码并轮换全部会话。

    平台强制策略开启时禁止关闭（403），避免运营账号被降级。

    异常:
        AuthError: 密码/验证码错误或锁定（401）、未启用（409）、策略禁止（403）、
            密文损坏（500，fail-closed）。
    """

    settings = get_settings()
    if realm == "platform" and settings.mfa_require_platform:
        raise AuthError("MFA_POLICY_REQUIRED", "当前要求必须启用多因素认证，不能关闭", 403)

    ip_hash = hash_client_fingerprint(ip)
    ua_hash = hash_client_fingerprint(user_agent)
    device_label = session_store.device_label_from_user_agent(user_agent)

    pair: TokenPair | None = None
    old_families: list[UUID] = []
    failure_reason: str | None = None
    failure_state: LockoutState | None = None
    recovery_used = False
    secret_invalid = False

    async with session_store.session_scope(realm=realm, tenant_id=tenant_id) as session:
        await session_store.acquire_user_lock(session, realm=realm, user_id=principal_user_id)
        credentials = (
            (
                await session.execute(
                    text(_SQL[realm]["select_credentials"]),
                    {"uid": principal_user_id, **_scope_params(realm, tenant_id)},
                )
            )
            .mappings()
            .one_or_none()
        )
        row = (
            (
                await session.execute(
                    text(_SQL[realm]["select_mfa_state"]),
                    {"uid": principal_user_id, **_scope_params(realm, tenant_id)},
                )
            )
            .mappings()
            .one_or_none()
        )
        now = datetime.now(UTC)
        if credentials is None or row is None or row["status"] != "active":
            failure_reason = "account_unavailable"
        elif is_account_locked(cast(datetime | None, row["locked_until"]), now):
            failure_reason = "locked"
        else:
            password_ok = await anyio.to_thread.run_sync(
                verify_password, current_password, str(credentials["password_hash"])
            )
            if not password_ok:
                failure_state = await _apply_login_failure(
                    session,
                    realm=realm,
                    tenant_id=tenant_id,
                    user_id=principal_user_id,
                    state=_lockout_state(row),
                    settings=settings,
                    now=now,
                )
                failure_reason = "password_mismatch"
            elif not bool(row["mfa_enabled"]) or not row["mfa_secret_enc"]:
                failure_reason = "mfa_disabled"

        if failure_reason is None and row is not None:
            try:
                kek = decode_kek(settings.encryption_kek)
                secret = decrypt_secret(
                    str(row["mfa_secret_enc"]),
                    kek=kek,
                    aad=mfa_aad(realm, principal_user_id),
                )
            except (SecretDecryptError, SecretEncryptionError):
                logger.error("MFA secret 解密失败（realm=%s）", realm)
                secret_invalid = True
            else:
                step = verify_totp(
                    secret,
                    code,
                    interval=settings.mfa_totp_interval_seconds,
                    window_steps=settings.mfa_totp_window_steps,
                    last_used_step=cast(int | None, row["mfa_last_used_step"]),
                )
                if step is not None:
                    updated = (
                        await session.execute(
                            text(_SQL[realm]["update_mfa_step"]),
                            {
                                "step": step,
                                "uid": principal_user_id,
                                **_scope_params(realm, tenant_id),
                            },
                        )
                    ).first()
                    if updated is None:
                        step = None
                if step is None:
                    recovery_used = await _consume_recovery_code(
                        session,
                        realm=realm,
                        tenant_id=tenant_id,
                        user_id=principal_user_id,
                        code=code,
                        subkey=recovery_code_subkey(kek),
                    )
                if step is None and not recovery_used:
                    failure_state = await _apply_login_failure(
                        session,
                        realm=realm,
                        tenant_id=tenant_id,
                        user_id=principal_user_id,
                        state=_lockout_state(row),
                        settings=settings,
                        now=now,
                    )
                    failure_reason = "invalid_mfa_code"

        if failure_reason is None and not secret_invalid and row is not None:
            version = int(
                await session.scalar(
                    text(_SQL[realm]["disable_mfa"]),
                    {"uid": principal_user_id, **_scope_params(realm, tenant_id)},
                )
            )
            await session.execute(
                text(_SQL[realm]["delete_recovery_codes"]),
                {"uid": principal_user_id, **_scope_params(realm, tenant_id)},
            )
            old_families = list(
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
                {
                    "uid": principal_user_id,
                    "reason": "mfa_change",
                    **_scope_params(realm, tenant_id),
                },
            )
            roles = await _load_roles(
                session, realm=realm, tenant_id=tenant_id, user_id=principal_user_id
            )
            pair = await _issue_tokens(
                settings,
                realm=realm,
                user_id=principal_user_id,
                tenant_id=tenant_id,
                token_version=version,
                must_change_password=bool(row["must_change_password"]),
                ip_hash=ip_hash,
                ua_hash=ua_hash,
                session=session,
                device_label=device_label,
                roles=roles,
                mfa_verified=False,
            )
            await append_audit(
                session,
                action="auth.mfa.disabled",
                resource_type="auth",
                actor_realm=realm,
                actor_id=principal_user_id,
                tenant_id=tenant_id,
                metadata={
                    "sessions_all_revoked": True,
                    "recovery_code_used": recovery_used,
                    "ip_hash": ip_hash,
                },
                request_id=request_id,
            )
            if recovery_used:
                await append_audit(
                    session,
                    action="auth.mfa.recovery_used",
                    resource_type="auth",
                    actor_realm=realm,
                    actor_id=principal_user_id,
                    tenant_id=tenant_id,
                    metadata={"phase": "disable", "ip_hash": ip_hash},
                    request_id=request_id,
                )
            await revocation.register_session(pair.session_id, realm=realm)

    if secret_invalid:
        await _audit_mfa_secret_invalid(
            realm=realm,
            tenant_id=tenant_id,
            user_id=principal_user_id,
            request_id=request_id,
        )
        raise AuthError("MFA_UNAVAILABLE", "多因素认证暂不可用，请稍后重试或联系管理员", 500)

    if failure_reason is not None:
        await _append_realm_audit(
            action="auth.mfa.disable_failed",
            realm=realm,
            tenant_id=tenant_id,
            user_id=principal_user_id,
            result="failure",
            metadata={
                "reason": failure_reason,
                "failed_attempts": (
                    failure_state.failed_attempts if failure_state is not None else None
                ),
                "lock_applied": (
                    failure_state.locked_until is not None if failure_state is not None else False
                ),
            },
            request_id=request_id,
        )
        if failure_reason == "mfa_disabled":
            raise AuthError("MFA_NOT_ENABLED", "多因素认证未启用", 409)
        if failure_reason == "account_unavailable":
            raise AuthError("ACCOUNT_UNAVAILABLE", "账号不可用，请联系管理员", 401)
        raise AuthError("INVALID_CREDENTIALS", "当前密码或验证码不正确", 401)

    if pair is None:
        raise RuntimeError("disable_mfa 未返回令牌")

    await revocation.delete_sessions(old_families, realm=realm)
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
        return bool(
            await session.scalar(text("select 1 from tenants where id = :tid"), {"tid": tenant_id})
        )


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
        failure.reason in {"reuse", "mfa_required"}
        and failure.family_id is not None
        and failure.user_id is not None
    ):
        # 先独立提交家族撤销（安全动作优先），审计失败不得连带回滚（审查 Minor 9）；
        # 取同一把用户锁，避免在途刷新插入的新行逃过撤销（第二轮审查 M3）。
        async with session_store.session_scope(realm=realm, tenant_id=tenant_id) as session:
            await session_store.acquire_user_lock(session, realm=realm, user_id=failure.user_id)
            await _revoke_family(
                session,
                realm=realm,
                tenant_id=tenant_id,
                user_id=failure.user_id,
                family_id=failure.family_id,
                reason="reuse_detected" if failure.reason == "reuse" else "mfa_change",
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
    if reason == "mfa_required":
        return AuthError("MFA_REQUIRED", "需要完成多因素认证，请重新登录", 401)
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
        family_revoked = await session.scalar(text(_SQL[realm]["family_revoked"]), params)
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
        (
            await session.execute(
                text(_SQL[realm]["select_refresh"]),
                {"jti": jti, **_scope_params(realm, tenant_id)},
            )
        )
        .mappings()
        .one_or_none()
    )

    if row is None or not hmac.compare_digest(str(row["token_hash"]), expected_hash):
        raise _RefreshFailure("unknown_token")

    # 与撤销/改密/其它刷新串行化；锁后重读，避免用旧快照轮换已撤销 family
    await session_store.acquire_user_lock(session, realm=realm, user_id=row["user_id"])
    row = (
        (
            await session.execute(
                text(_SQL[realm]["select_refresh"]),
                {"jti": jti, **_scope_params(realm, tenant_id)},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None or not hmac.compare_digest(str(row["token_hash"]), expected_hash):
        raise _RefreshFailure("unknown_token")

    if row["revoked_at"] is not None:
        raise await _revoked_failure(session, realm=realm, tenant_id=tenant_id, row=row)

    if row["expires_at"] <= datetime.now(UTC):
        raise _RefreshFailure("expired", family_id=row["family_id"], user_id=row["user_id"])

    user_row = (
        (
            await session.execute(
                text(_SQL[realm]["select_user_state"]),
                {"uid": row["user_id"], **_scope_params(realm, tenant_id)},
            )
        )
        .mappings()
        .one_or_none()
    )
    if user_row is None or user_row["status"] != "active":
        raise _RefreshFailure(
            "account_unavailable", family_id=row["family_id"], user_id=row["user_id"]
        )

    # 强制 MFA 策略上线前的旧会话（或策略开启后仅密码登录的会话）不得继续续期
    if realm == "platform" and settings.mfa_require_platform and not bool(row["mfa_verified"]):
        raise _RefreshFailure("mfa_required", family_id=row["family_id"], user_id=row["user_id"])

    roles = await _load_roles(session, realm=realm, tenant_id=tenant_id, user_id=row["user_id"])
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
            (
                await session.execute(
                    text(_SQL[realm]["select_refresh"]),
                    {"jti": jti, **_scope_params(realm, tenant_id)},
                )
            )
            .mappings()
            .one_or_none()
        )
        if current is None:
            raise _RefreshFailure("unknown_token")
        raise await _revoked_failure(session, realm=realm, tenant_id=tenant_id, row=current)

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
        "mfa_verified": bool(row["mfa_verified"]),
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
        amr=("pwd", "totp") if bool(row["mfa_verified"]) else ("pwd",),
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
        mfa_verified=bool(row["mfa_verified"]),
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
                "subject_user_id": (str(subject_user_id) if subject_user_id is not None else None),
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
        (
            await session.execute(
                text(_SQL[realm]["family_by_jti"]),
                {"jti": jti, **_scope_params(realm, tenant_id)},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None or not hmac.compare_digest(str(row["token_hash"]), expected_hash):
        return None

    # 与刷新串行化后重读：避免登出撤销与轮换交错时漏掉新插入的行
    await session_store.acquire_user_lock(session, realm=realm, user_id=row["user_id"])
    row = (
        (
            await session.execute(
                text(_SQL[realm]["family_by_jti"]),
                {"jti": jti, **_scope_params(realm, tenant_id)},
            )
        )
        .mappings()
        .one_or_none()
    )
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
    mfa_verified: bool = False,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> TokenPair:
    """修改密码：校验旧密码 → 更新哈希 → 撤销全部家族 → 重新签发令牌。

    `mfa_verified` 来自当前访问令牌的 `amr`：改密重签时原样保留，避免把已完成
    MFA 的会话降级为仅密码会话（强制策略窗口内）。

    异常:
        AuthError: 旧密码错误（401）、新密码不合规（422）、账号不可用（401）。
    """

    if new_password == current_password:
        raise _weak_password("新密码不能与当前密码相同")

    settings = get_settings()
    ip_hash = hash_client_fingerprint(ip)
    ua_hash = hash_client_fingerprint(user_agent)
    device_label = session_store.device_label_from_user_agent(user_agent)

    pair: TokenPair | None = None
    old_families: list[UUID] = []
    failure: _RefreshFailure | None = None
    async with session_store.session_scope(realm=realm, tenant_id=tenant_id) as session:
        try:
            pair, old_families = await _change_password_in_session(
                session,
                settings=settings,
                realm=realm,
                tenant_id=tenant_id,
                principal_user_id=principal_user_id,
                current_password=current_password,
                new_password=new_password,
                mfa_verified=mfa_verified,
                ip_hash=ip_hash,
                ua_hash=ua_hash,
                device_label=device_label,
                request_id=request_id,
            )
        except _RefreshFailure as exc:
            # 捕获后正常退出事务：失败计数必须随事务提交，不能被异常回滚
            failure = exc

    if failure is not None:
        await _record_password_failure(
            realm=realm,
            tenant_id=tenant_id,
            user_id=principal_user_id,
            reason=failure.reason,
            request_id=request_id,
            ip_hash=ip_hash,
            ua_hash=ua_hash,
        )
        if failure.reason in {"password_mismatch", "locked"}:
            # 锁定与密码错误对外同文案，避免泄露账号锁定状态
            raise AuthError("INVALID_CREDENTIALS", "当前密码不正确", 401) from failure
        raise AuthError("ACCOUNT_UNAVAILABLE", "账号不可用，请联系管理员", 401) from failure

    if pair is None:
        raise RuntimeError("change_password 未返回令牌")

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
    mfa_verified: bool,
    ip_hash: str | None,
    ua_hash: str | None,
    device_label: str | None,
    request_id: str | None,
) -> tuple[TokenPair, list[UUID]]:
    """改密主体逻辑（同一事务内：改哈希、自增版本、撤销全部家族、重签令牌）。

    返回值:
        (新令牌, 被撤销的旧 family id 列表)；后者用于事务提交后清理 Redis 标记。
    """

    await session_store.acquire_user_lock(session, realm=realm, user_id=principal_user_id)
    row = (
        (
            await session.execute(
                text(_SQL[realm]["select_credentials"]),
                {"uid": principal_user_id, **_scope_params(realm, tenant_id)},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["status"] != "active":
        raise _RefreshFailure("account_unavailable", user_id=principal_user_id)

    now = datetime.now(UTC)
    if is_account_locked(cast(datetime | None, row["locked_until"]), now):
        # 锁定期间不再验证口令，也不继续累加（与登录侧一致）
        raise _RefreshFailure("locked", user_id=principal_user_id)

    old_ok = await anyio.to_thread.run_sync(
        verify_password, current_password, str(row["password_hash"])
    )
    if not old_ok:
        # 失败计数在同一事务内更新，由外层捕获信号后提交；审计在事务外单独写
        await _apply_login_failure(
            session,
            realm=realm,
            tenant_id=tenant_id,
            user_id=principal_user_id,
            state=LockoutState(
                failed_attempts=int(row["failed_login_attempts"]),
                window_started_at=cast(datetime | None, row["failed_login_window_started_at"]),
                locked_until=cast(datetime | None, row["locked_until"]),
            ),
            settings=settings,
            now=now,
        )
        raise _RefreshFailure("password_mismatch", user_id=principal_user_id)

    _validate_new_password(new_password, login_id=str(row["login_id"]))
    await _ensure_password_not_reused(
        session,
        realm=realm,
        tenant_id=tenant_id,
        user_id=principal_user_id,
        new_password=new_password,
        current_hash=str(row["password_hash"]),
        history_size=settings.auth_password_history_size,
    )
    await _record_password_history(
        session,
        realm=realm,
        tenant_id=tenant_id,
        user_id=principal_user_id,
        old_hash=str(row["password_hash"]),
        keep=settings.auth_password_history_size,
    )

    new_hash = await anyio.to_thread.run_sync(hash_password, new_password)
    await session.execute(
        text(_SQL[realm]["set_credential_hash"]),
        {"hash": new_hash, "uid": principal_user_id, **_scope_params(realm, tenant_id)},
    )
    # 改密后邮件里的未使用重置链接一并作废（否则旧链接仍可再次改密）
    await session.execute(
        text(_SQL[realm]["invalidate_reset_tokens"]),
        {"uid": principal_user_id, **_scope_params(realm, tenant_id)},
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
        {"uid": principal_user_id, "reason": "password_change", **_scope_params(realm, tenant_id)},
    )

    version_row: int = (
        await session.execute(
            text(_SQL[realm]["select_credential_version"]),
            {"uid": principal_user_id, **_scope_params(realm, tenant_id)},
        )
    ).scalar_one()

    roles = await _load_roles(session, realm=realm, tenant_id=tenant_id, user_id=principal_user_id)
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
        mfa_verified=mfa_verified,
    )
    await append_audit(
        session,
        action="auth.password.changed",
        resource_type="auth",
        actor_realm=realm,
        actor_id=principal_user_id,
        tenant_id=tenant_id,
        metadata={
            "sessions_all_revoked": True,
            "ip_hash": ip_hash,
            "ua_hash": ua_hash,
        },
        request_id=request_id,
    )
    await revocation.register_session(pair.session_id, realm=realm)
    return pair, old_families


def _build_reset_url(base: str, token: str) -> str:
    """拼接邮件里的重置链接。

    令牌放 URL fragment：浏览器不会把它发给服务器/代理，也不进 Referer；
    前端从 `location.hash` 读取后自行调用 confirm 接口。
    """

    return f"{base}#token={quote(token, safe='')}"


def _mask_login(login: str) -> str:
    """登录名脱敏（审计不留明文登录名）。"""

    return f"{login[:1]}***" if login else ""


async def _audit_reset_request(
    session: AsyncSession,
    *,
    realm: Realm,
    tenant_id: UUID | None,
    reason: str,
    result: str,
    request_id: str | None,
    login_masked: str = "",
    subject_user_id: UUID | None = None,
    email_sent: bool = False,
    ip_hash: str | None = None,
) -> None:
    """写重置申请审计（不记录邮箱/令牌明文）。"""

    metadata: dict[str, object] = {
        "reason": reason,
        "email_sent": email_sent,
        "ip_hash": ip_hash,
    }
    if login_masked:
        metadata["login_masked"] = login_masked
    if subject_user_id is not None:
        metadata["subject_user_id"] = str(subject_user_id)
    await append_audit(
        session,
        action="auth.password.reset_requested",
        resource_type="auth",
        actor_realm="system",
        tenant_id=tenant_id if realm == "tenant" else None,
        result=result,
        metadata=metadata,
        request_id=request_id,
    )


async def _audit_unknown_reset(
    *,
    realm: Realm,
    tenant_id: UUID | None,
    login_masked: str,
    ip_hash: str | None,
    request_id: str | None,
) -> None:
    """未知账号审计固定落平台，避免未认证请求向任意租户审计流灌数据。"""

    metadata: dict[str, object] = {
        "reason": "unknown_account",
        "realm": realm,
        "login_masked": login_masked,
        "ip_hash": ip_hash,
    }
    if tenant_id is not None:
        metadata["attempted_tenant_id"] = str(tenant_id)
    async with db_session(platform=True) as session:
        await append_audit(
            session,
            action="auth.password.reset_requested",
            resource_type="auth",
            actor_realm="system",
            result="failure",
            metadata=metadata,
            request_id=request_id,
        )


async def _request_reset(
    *,
    realm: Realm,
    tenant_id: UUID | None,
    login: str,
    settings: Settings,
    ip_hash: str | None,
    request_id: str | None,
) -> None:
    """重置申请主体逻辑（恒不抛账号相关错误，只写审计）。

    - 账号不存在/停用/无邮箱/冷却中：不发送、不泄露；
    - 令牌行先随事务提交，**提交成功后才发邮件**，避免"已收到链接但令牌被回滚"；
    - 邮件发送失败：独立事务作废已提交令牌并写失败审计（接口仍返回 202）。
    """

    login_masked = _mask_login(login)
    subject_user_id: UUID | None = None
    prepared: _PreparedReset | None = None
    async with session_store.session_scope(realm=realm, tenant_id=tenant_id) as session:
        row = (
            (
                await session.execute(
                    text(_SQL[realm]["select_reset_user_by_login"]),
                    {"login": login, **_scope_params(realm, tenant_id)},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            await _audit_unknown_reset(
                realm=realm,
                tenant_id=tenant_id,
                login_masked=login_masked,
                ip_hash=ip_hash,
                request_id=request_id,
            )
            return

        subject_user_id = cast(UUID, row["id"])
        await session_store.acquire_user_lock(session, realm=realm, user_id=subject_user_id)
        # 锁后重读：并发改密/停用后不向旧状态发信
        row = (
            (
                await session.execute(
                    text(_SQL[realm]["select_reset_user_by_login"]),
                    {"login": login, **_scope_params(realm, tenant_id)},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            await _audit_unknown_reset(
                realm=realm,
                tenant_id=tenant_id,
                login_masked=login_masked,
                ip_hash=ip_hash,
                request_id=request_id,
            )
            return
        if row["status"] != "active":
            await _audit_reset_request(
                session,
                realm=realm,
                tenant_id=tenant_id,
                reason="account_unavailable",
                result="failure",
                login_masked=login_masked,
                subject_user_id=subject_user_id,
                ip_hash=ip_hash,
                request_id=request_id,
            )
            return
        if realm == "tenant" and row["user_type"] == "student":
            # 学生自助重置依赖 M1-E 的租户 RBAC/身份核验，M2 才开放
            await _audit_reset_request(
                session,
                realm=realm,
                tenant_id=tenant_id,
                reason="student_reset_deferred",
                result="failure",
                login_masked=login_masked,
                subject_user_id=subject_user_id,
                ip_hash=ip_hash,
                request_id=request_id,
            )
            return
        email = str(row["email"]) if row["email"] else None
        if email is None:
            await _audit_reset_request(
                session,
                realm=realm,
                tenant_id=tenant_id,
                reason="missing_email",
                result="failure",
                login_masked=login_masked,
                subject_user_id=subject_user_id,
                ip_hash=ip_hash,
                request_id=request_id,
            )
            return

        now = datetime.now(UTC)
        last_created = await session.scalar(
            text(_SQL[realm]["latest_reset_created_at"]),
            {"uid": subject_user_id, **_scope_params(realm, tenant_id)},
        )
        if last_created is not None and now - last_created < timedelta(
            seconds=settings.password_reset_request_cooldown_seconds
        ):
            await _audit_reset_request(
                session,
                realm=realm,
                tenant_id=tenant_id,
                reason="cooldown",
                result="failure",
                login_masked=login_masked,
                subject_user_id=subject_user_id,
                ip_hash=ip_hash,
                request_id=request_id,
            )
            return

        token = generate_password_reset_token(realm, tenant_id)
        _, _, secret = parse_password_reset_token(token)
        expires_at = now + timedelta(seconds=settings.password_reset_token_ttl_seconds)
        params: dict[str, object] = {
            "uid": subject_user_id,
            "hash": hash_password_reset_token(secret),
            "expires_at": expires_at,
            "ip_hash": ip_hash,
            **_scope_params(realm, tenant_id),
        }
        # 先作废旧令牌再插新令牌：旧链接立即失效（同一事务，失败一起回滚）
        await session.execute(
            text(_SQL[realm]["invalidate_reset_tokens"]),
            {"uid": subject_user_id, **_scope_params(realm, tenant_id)},
        )
        await session.execute(text(_SQL[realm]["insert_reset_token"]), params)
        prepared = _PreparedReset(email=email, token=token)

    if prepared is None:
        return

    subject, body = render_password_reset_email(
        reset_url=_build_reset_url(settings.password_reset_url_base, prepared.token),
        expires_minutes=max(1, settings.password_reset_token_ttl_seconds // 60),
    )
    try:
        await build_mailer(settings).send(to=prepared.email, subject=subject, body=body)
    except Exception:
        logger.warning("密码重置邮件发送失败（realm=%s）", realm)
        if realm == "tenant" and tenant_id is not None and not await _tenant_exists(tenant_id):
            return
        if subject_user_id is not None:
            async with session_store.session_scope(
                realm=realm, tenant_id=tenant_id
            ) as audit_session:
                # 令牌已随事务提交：投递失败必须显式作废，避免留下无人可用的有效链接
                await audit_session.execute(
                    text(_SQL[realm]["invalidate_reset_tokens"]),
                    {"uid": subject_user_id, **_scope_params(realm, tenant_id)},
                )
                await _audit_reset_request(
                    audit_session,
                    realm=realm,
                    tenant_id=tenant_id,
                    reason="email_delivery_failed",
                    result="failure",
                    login_masked=login_masked,
                    subject_user_id=subject_user_id,
                    ip_hash=ip_hash,
                    request_id=request_id,
                )
        return

    if realm == "tenant" and tenant_id is not None and not await _tenant_exists(tenant_id):
        return
    async with session_store.session_scope(realm=realm, tenant_id=tenant_id) as audit_session:
        await _audit_reset_request(
            audit_session,
            realm=realm,
            tenant_id=tenant_id,
            reason="sent",
            result="success",
            login_masked=login_masked,
            subject_user_id=subject_user_id,
            email_sent=True,
            ip_hash=ip_hash,
            request_id=request_id,
        )


async def request_password_reset(
    *,
    realm: Realm,
    school_code: str | None = None,
    login_id: str | None = None,
    account: str | None = None,
    ip: str | None = None,
    request_id: str | None = None,
) -> None:
    """申请密码重置（邮件发送；账号存在性不影响响应）。

    租户 realm 需 `school_code + login_id`；平台 realm 需 `account`。
    账号不存在、无邮箱、冷却中、邮件失败均不向调用方报错，只写审计。
    """

    settings = get_settings()
    ip_hash = hash_client_fingerprint(ip)
    if ip_hash is not None and not await revocation.allow_rate_limit(
        "password_reset",
        ip_hash,
        limit=settings.password_reset_ip_limit,
        window_seconds=settings.password_reset_ip_window_seconds,
    ):
        # 限流命中静默返回（接口仍 202）：写审计会放大未认证请求的磁盘/写入压力
        return
    if realm == "tenant":
        code = (school_code or "").strip().upper()
        async with db_session(platform=True) as platform_session:
            tenant_row = (
                (
                    await platform_session.execute(
                        text("select id, status from tenants where school_code = :code"),
                        {"code": code},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if tenant_row is None or tenant_row["status"] != "active":
            # 学校不存在/停用：平台级审计，不暴露给调用方
            async with db_session(platform=True) as audit_session:
                await append_audit(
                    audit_session,
                    action="auth.password.reset_requested",
                    resource_type="auth",
                    actor_realm="system",
                    result="failure",
                    metadata={
                        "reason": "tenant_unavailable",
                        "school_code": code,
                        "ip_hash": ip_hash,
                    },
                    request_id=request_id,
                )
            return
        await _request_reset(
            realm="tenant",
            tenant_id=cast(UUID, tenant_row["id"]),
            login=(login_id or "").strip(),
            settings=settings,
            ip_hash=ip_hash,
            request_id=request_id,
        )
        return
    await _request_reset(
        realm="platform",
        tenant_id=None,
        login=(account or "").strip(),
        settings=settings,
        ip_hash=ip_hash,
        request_id=request_id,
    )


async def _record_reset_failure(
    *,
    realm: Realm,
    tenant_id: UUID | None,
    reason: str,
    token_found: bool,
    request_id: str | None,
    ip_hash: str | None = None,
    ua_hash: str | None = None,
) -> None:
    """重置确认失败审计（独立事务）。

    只有令牌确实存在于该租户时才写租户审计；伪造的 tenant_id/随机令牌
    降级为平台事件，避免向任意租户的审计流灌数据。
    """

    metadata: dict[str, object] = {
        "reason": reason,
        "ip_hash": ip_hash,
        "ua_hash": ua_hash,
    }
    if realm == "tenant" and tenant_id is not None:
        if token_found and await _tenant_exists(tenant_id):
            async with session_store.session_scope(realm="tenant", tenant_id=tenant_id) as session:
                await append_audit(
                    session,
                    action="auth.password.reset_failed",
                    resource_type="auth",
                    actor_realm="system",
                    tenant_id=tenant_id,
                    result="failure",
                    metadata=metadata,
                    request_id=request_id,
                )
            return
        metadata["attempted_tenant_id"] = str(tenant_id)
    async with db_session(platform=True) as session:
        await append_audit(
            session,
            action="auth.password.reset_failed",
            resource_type="auth",
            actor_realm="system",
            result="failure",
            metadata=metadata,
            request_id=request_id,
        )


async def _confirm_reset_in_session(
    session: AsyncSession,
    *,
    settings: Settings,
    realm: Realm,
    tenant_id: UUID | None,
    expected_hash: str,
    new_password: str,
    ip_hash: str | None,
    ua_hash: str | None,
    request_id: str | None,
) -> list[UUID]:
    """确认重置（同一事务：令牌校验/单次消费、策略/历史、改密、撤销会话、清锁）。

    任何失败都抛 `_ResetFailure`，令牌消费与改密一起回滚，令牌可继续重试。
    返回值是事务提交后需要清理 Redis 标记的旧 family id 列表。
    """

    row = (
        (
            await session.execute(
                text(_SQL[realm]["select_reset_by_hash"]),
                {"hash": expected_hash, **_scope_params(realm, tenant_id)},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise _ResetFailure("invalid_token")

    user_id = cast(UUID, row["user_id"])
    await session_store.acquire_user_lock(session, realm=realm, user_id=user_id)
    # 锁后重读：并发确认/新申请作废时以最新状态为准
    row = (
        (
            await session.execute(
                text(_SQL[realm]["select_reset_by_hash"]),
                {"hash": expected_hash, **_scope_params(realm, tenant_id)},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise _ResetFailure("invalid_token")
    if row["used_at"] is not None:
        raise _ResetFailure("used", token_found=True)
    now = datetime.now(UTC)
    if cast(datetime, row["expires_at"]) <= now:
        raise _ResetFailure("expired", token_found=True)

    credentials = (
        (
            await session.execute(
                text(_SQL[realm]["select_credentials"]),
                {"uid": user_id, **_scope_params(realm, tenant_id)},
            )
        )
        .mappings()
        .one_or_none()
    )
    if credentials is None or credentials["status"] != "active":
        raise _ResetFailure("account_unavailable", token_found=True)

    try:
        validate_password_strength(new_password, login_id=str(credentials["login_id"]))
    except PasswordPolicyError as exc:
        raise _ResetFailure("weak_password", message=str(exc), token_found=True) from exc
    try:
        await _ensure_password_not_reused(
            session,
            realm=realm,
            tenant_id=tenant_id,
            user_id=user_id,
            new_password=new_password,
            current_hash=str(credentials["password_hash"]),
            history_size=settings.auth_password_history_size,
        )
    except AuthError as exc:
        raise _ResetFailure("weak_password", message=exc.message, token_found=True) from exc

    await _record_password_history(
        session,
        realm=realm,
        tenant_id=tenant_id,
        user_id=user_id,
        old_hash=str(credentials["password_hash"]),
        keep=settings.auth_password_history_size,
    )
    new_hash = await anyio.to_thread.run_sync(hash_password, new_password)
    await session.execute(
        text(_SQL[realm]["set_credential_hash"]),
        {"hash": new_hash, "uid": user_id, **_scope_params(realm, tenant_id)},
    )

    consumed = (
        await session.execute(
            text(_SQL[realm]["consume_reset_token"]),
            {"id": row["id"], **_scope_params(realm, tenant_id)},
        )
    ).first()
    if consumed is None:
        raise _ResetFailure("used", token_found=True)
    # 用掉一个即作废该账号其余未用令牌（单账号同时只保留一条可用链接）
    await session.execute(
        text(_SQL[realm]["invalidate_reset_tokens"]),
        {"uid": user_id, **_scope_params(realm, tenant_id)},
    )

    old_families: list[UUID] = list(
        (
            await session.execute(
                text(_SQL[realm]["active_families"]),
                {"uid": user_id, **_scope_params(realm, tenant_id)},
            )
        )
        .scalars()
        .all()
    )
    await session.execute(
        text(_SQL[realm]["revoke_user_tokens"]),
        {"uid": user_id, "reason": "password_reset", **_scope_params(realm, tenant_id)},
    )

    await append_audit(
        session,
        action="auth.password.reset",
        resource_type="auth",
        actor_realm=realm,
        actor_id=user_id,
        tenant_id=tenant_id if realm == "tenant" else None,
        metadata={
            "sessions_all_revoked": True,
            # `old_families` 已收窄为"还活着的家族"，所以这个数就是真正被断开的设备数
            "sessions_revoked": len(old_families),
            "ip_hash": ip_hash,
            "ua_hash": ua_hash,
        },
        request_id=request_id,
    )
    return old_families


async def confirm_password_reset(
    *,
    token: str,
    new_password: str,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> None:
    """用邮件令牌重置密码；成功后要求重新登录（不签发任何令牌）。

    异常:
        AuthError: 令牌无效/过期/已使用（400）、新密码不合规或复用（422）。
    """

    settings = get_settings()
    ip_hash = hash_client_fingerprint(ip)
    if ip_hash is not None and not await revocation.allow_rate_limit(
        "password_reset_confirm",
        ip_hash,
        limit=settings.password_reset_confirm_ip_limit,
        window_seconds=settings.password_reset_ip_window_seconds,
    ):
        # 限流命中不再写失败审计：否则未认证请求仍能放大审计/磁盘
        raise AuthError("RATE_LIMITED", "请求过于频繁，请稍后再试", 429)

    try:
        realm, tenant_id, secret = parse_password_reset_token(token)
    except TokenError as exc:
        raise AuthError("INVALID_RESET_TOKEN", "重置链接无效或已过期", 400) from exc

    expected_hash = hash_password_reset_token(secret)
    ua_hash = hash_client_fingerprint(user_agent)
    try:
        async with session_store.session_scope(realm=realm, tenant_id=tenant_id) as session:
            old_families = await _confirm_reset_in_session(
                session,
                settings=settings,
                realm=realm,
                tenant_id=tenant_id,
                expected_hash=expected_hash,
                new_password=new_password,
                ip_hash=ip_hash,
                ua_hash=ua_hash,
                request_id=request_id,
            )
    except _ResetFailure as failure:
        await _record_reset_failure(
            realm=realm,
            tenant_id=tenant_id,
            reason=failure.reason,
            token_found=failure.token_found,
            request_id=request_id,
            ip_hash=ip_hash,
            ua_hash=ua_hash,
        )
        if failure.reason == "weak_password":
            raise AuthError(
                "WEAK_PASSWORD", failure.message or "新密码不符合安全要求", 422
            ) from failure
        raise AuthError("INVALID_RESET_TOKEN", "重置链接无效或已过期", 400) from failure

    # 事务已提交：删除旧设备的会话标记（token_version 已自增，访问令牌同步失效）
    await revocation.delete_sessions(old_families, realm=realm)
