"""访问令牌（JWT）与刷新令牌（不透明随机串）。

- **访问令牌**：HS256 JWT，15 分钟；claim 含 `sub/realm/tenant_id/jti/sid/token_version/
  must_change_password/iss/aud/exp/kid`。`sid` 是刷新令牌家族 id（会话），用于被踢/下线
  后立即失效。校验时校验签名、`iss`、`exp` 与**按 realm 对应的 audience**
  （TD-074：租户与平台令牌不可混用）。
- **刷新令牌**：`secrets.token_urlsafe(48)` 随机串，数据库只存 SHA-256 十六进制哈希；
  对外只出现一次（响应体），不落日志/审计。
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

import jwt as pyjwt

from app.config import Settings

Realm = Literal["tenant", "platform"]

# 刷新令牌随机字节数（token_urlsafe 会做 base64 编码，熵为 8×48 位）
REFRESH_TOKEN_BYTES = 48


class TokenError(Exception):
    """令牌无效/过期/受众不符。"""

    def __init__(self, code: str, message: str) -> None:
        """初始化错误。

        参数:
            code: 机器可读错误码（`TOKEN_INVALID`/`TOKEN_EXPIRED`）。
            message: 面向用户的说明（不含令牌内容）。
        """

        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AccessClaims:
    """访问令牌 claim 的解析结果。"""

    subject: UUID
    realm: Realm
    tenant_id: UUID | None
    jti: UUID
    session_id: UUID
    token_version: int
    must_change_password: bool
    expires_at: datetime


def audience_for(settings: Settings, realm: Realm) -> str:
    """返回 realm 对应的 audience。"""

    return settings.jwt_audience_platform if realm == "platform" else settings.jwt_audience_tenant


def create_access_token(
    settings: Settings,
    *,
    user_id: UUID,
    realm: Realm,
    tenant_id: UUID | None,
    family_id: UUID,
    token_version: int,
    must_change_password: bool,
    now: datetime | None = None,
) -> tuple[str, AccessClaims]:
    """签发访问令牌。

    参数:
        settings: 应用配置（含密钥/issuer/audience/TTL）。
        user_id: 用户主体（租户用户或平台账号 id）。
        realm: `tenant` 或 `platform`。
        tenant_id: 租户 realm 必填；平台 realm 必须为 None。
        family_id: 刷新令牌家族 id（访问令牌 `sid`，用于会话级即时失效）。
        token_version: 用户当前版本号（权限/状态变更后自增，旧令牌立即失效）。
        must_change_password: 是否处于"首登必须改密"状态。
        now: 可选当前时间（测试注入）。

    返回值:
        (token, claims)。

    异常:
        ValueError: realm 与 tenant_id 组合非法。
    """

    if realm == "tenant" and tenant_id is None:
        raise ValueError("租户 realm 的访问令牌必须有 tenant_id")
    if realm == "platform" and tenant_id is not None:
        raise ValueError("平台 realm 的访问令牌不得携带 tenant_id")

    issued = now or datetime.now(UTC)
    expires_at = issued + timedelta(seconds=settings.access_token_ttl_seconds)
    jti = uuid4()
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "realm": realm,
        "tenant_id": str(tenant_id) if tenant_id else None,
        "jti": str(jti),
        "sid": str(family_id),
        "token_version": token_version,
        "must_change_password": must_change_password,
        "iss": settings.jwt_issuer,
        "aud": audience_for(settings, realm),
        "iat": int(issued.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    encoded = pyjwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
        headers={"kid": settings.jwt_active_kid},
    )
    claims = AccessClaims(
        subject=user_id,
        realm=realm,
        tenant_id=tenant_id,
        jti=jti,
        session_id=family_id,
        token_version=token_version,
        must_change_password=must_change_password,
        expires_at=expires_at,
    )
    return encoded, claims


def decode_access_token(settings: Settings, token: str) -> AccessClaims:
    """校验并解析访问令牌。

    参数:
        settings: 应用配置。
        token: 待校验的访问令牌。

    返回值:
        AccessClaims。

    异常:
        TokenError: 签名/格式/受众/过期校验失败。
    """

    try:
        payload = pyjwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            # aud 在下面按 realm 手工校验：解码前不知道 realm（PyJWT 在 aud 存在但未传
            # audience 时会直接报 InvalidAudienceError，故先关闭内置校验）
            options={
                "require": ["exp", "iss", "aud", "sub", "jti", "sid"],
                "verify_aud": False,
            },
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise TokenError("TOKEN_EXPIRED", "登录已过期，请重新登录") from exc
    except pyjwt.InvalidTokenError as exc:
        raise TokenError("TOKEN_INVALID", "令牌无效") from exc

    realm = payload.get("realm")
    if realm not in ("tenant", "platform"):
        raise TokenError("TOKEN_INVALID", "令牌无效")
    expected_audience = audience_for(settings, realm)
    audience = payload.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if expected_audience not in audiences:
        # 跨 realm 混用（例如拿平台令牌访问租户接口）→ 拒绝（TD-074）
        raise TokenError("TOKEN_INVALID", "令牌无效")

    raw_tenant = payload.get("tenant_id")
    tenant_id: UUID | None = None
    if raw_tenant:
        try:
            tenant_id = UUID(str(raw_tenant))
        except ValueError as exc:
            raise TokenError("TOKEN_INVALID", "令牌无效") from exc
    try:
        return AccessClaims(
            subject=UUID(str(payload["sub"])),
            realm=realm,
            tenant_id=tenant_id,
            jti=UUID(str(payload["jti"])),
            session_id=UUID(str(payload["sid"])),
            token_version=int(payload.get("token_version", -1)),
            must_change_password=bool(payload.get("must_change_password", True)),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise TokenError("TOKEN_INVALID", "令牌无效") from exc


def generate_refresh_token() -> str:
    """生成刷新令牌明文（只在响应中返回一次）。"""

    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def hash_refresh_token(token: str) -> str:
    """刷新令牌哈希（数据库只存哈希，明文不落库/日志）。"""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_client_fingerprint(value: str | None) -> str | None:
    """对 UA/IP 做不可逆哈希（审计与异常检测用，不落明文）。"""

    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
