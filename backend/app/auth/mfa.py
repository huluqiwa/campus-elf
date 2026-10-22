"""MFA 临时令牌：登录挑战与强制绑定。

两类令牌都是 HS256 JWT，`aud=JWT_AUDIENCE_MFA`（与访问令牌的 realm audience 分离，
访问令牌解码器会拒绝它们），`purpose` 区分用途：

- `mfa_challenge`：密码正确且已绑定 MFA 后签发，只能调用 `/auth/mfa/verify`；
- `mfa_enroll`：平台强制策略下尚未绑定 MFA 时签发，只能调用 `/auth/mfa/enroll|activate`。

令牌不写 Cookie、不入日志；服务端用 Redis 记录挑战 jti 的尝试次数与单次使用状态。
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

import jwt as pyjwt

from app.auth.tokens import Realm, TokenError
from app.config import Settings

MfaPurpose = Literal["mfa_challenge", "mfa_enroll"]

MFA_INVALID_MESSAGE = "登录状态已失效，请重新登录"


@dataclass(frozen=True, slots=True)
class MfaClaims:
    """MFA 临时令牌的解析结果。"""

    subject: UUID
    realm: Realm
    tenant_id: UUID | None
    purpose: MfaPurpose
    jti: UUID
    token_version: int
    expires_at: datetime


def _ttl_seconds(settings: Settings, purpose: MfaPurpose) -> int:
    if purpose == "mfa_challenge":
        return settings.mfa_challenge_ttl_seconds
    return settings.mfa_enroll_token_ttl_seconds


def create_mfa_token(
    settings: Settings,
    *,
    purpose: MfaPurpose,
    user_id: UUID,
    realm: Realm,
    tenant_id: UUID | None,
    token_version: int,
    now: datetime | None = None,
) -> tuple[str, UUID]:
    """签发 MFA 临时令牌，返回 (token, jti)。"""

    if realm == "tenant" and tenant_id is None:
        raise ValueError("租户 realm 的 MFA 令牌必须有 tenant_id")
    if realm == "platform" and tenant_id is not None:
        raise ValueError("平台 realm 的 MFA 令牌不得携带 tenant_id")

    issued = now or datetime.now(UTC)
    expires_at = issued + timedelta(seconds=_ttl_seconds(settings, purpose))
    jti = uuid4()
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "realm": realm,
        "tenant_id": str(tenant_id) if tenant_id else None,
        "purpose": purpose,
        "jti": str(jti),
        "token_version": token_version,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience_mfa,
        "iat": int(issued.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    encoded = pyjwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
        headers={"kid": settings.jwt_active_kid},
    )
    return encoded, jti


def decode_mfa_token(settings: Settings, token: str, *, expected_purpose: MfaPurpose) -> MfaClaims:
    """校验并解析 MFA 临时令牌。

    异常:
        TokenError: 签名/格式/受众/用途/过期校验失败（统一对外文案）。
    """

    try:
        payload = pyjwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience_mfa,
            options={"require": ["exp", "iss", "aud", "sub", "jti", "purpose"]},
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise TokenError("TOKEN_INVALID", MFA_INVALID_MESSAGE) from exc
    except pyjwt.InvalidTokenError as exc:
        raise TokenError("TOKEN_INVALID", MFA_INVALID_MESSAGE) from exc

    realm = payload.get("realm")
    if realm not in ("tenant", "platform"):
        raise TokenError("TOKEN_INVALID", MFA_INVALID_MESSAGE)
    if payload.get("purpose") != expected_purpose:
        raise TokenError("TOKEN_INVALID", MFA_INVALID_MESSAGE)

    raw_tenant = payload.get("tenant_id")
    tenant_id: UUID | None = None
    if raw_tenant:
        try:
            tenant_id = UUID(str(raw_tenant))
        except ValueError as exc:
            raise TokenError("TOKEN_INVALID", MFA_INVALID_MESSAGE) from exc
    if realm == "tenant" and tenant_id is None:
        raise TokenError("TOKEN_INVALID", MFA_INVALID_MESSAGE)
    # 与 create_mfa_token 的签发约束对称：平台令牌的 tenant_id 必须为空
    if realm == "platform" and tenant_id is not None:
        raise TokenError("TOKEN_INVALID", MFA_INVALID_MESSAGE)

    try:
        return MfaClaims(
            subject=UUID(str(payload["sub"])),
            realm=realm,
            tenant_id=tenant_id,
            purpose=expected_purpose,
            jti=UUID(str(payload["jti"])),
            token_version=int(payload["token_version"]),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise TokenError("TOKEN_INVALID", MFA_INVALID_MESSAGE) from exc
