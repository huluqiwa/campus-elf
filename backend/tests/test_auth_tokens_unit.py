"""M1-C 单元测试：访问令牌、刷新令牌格式与主体判定。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt as pyjwt
import pytest

from app.auth.mfa import create_mfa_token, decode_mfa_token
from app.auth.tokens import (
    TokenError,
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_client_fingerprint,
    hash_refresh_token,
)
from app.config import Settings

JWT_SECRET = "unit-test-jwt-secret-0123456789abcdef"


def _settings(**overrides: object) -> Settings:
    """构造测试配置（不读 .env）。"""

    base: dict[str, object] = {
        "_env_file": None,
        "redis_url": "redis://127.0.0.1:6379/15",
        "jwt_secret": JWT_SECRET,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestAccessToken:
    """访问令牌签发与校验。"""

    def test_tenant_token_roundtrip(self) -> None:
        """租户令牌可解析且 claim 完整。"""

        settings = _settings()
        tenant_id, user_id = uuid4(), uuid4()
        token, claims = create_access_token(
            settings,
            user_id=user_id,
            realm="tenant",
            tenant_id=tenant_id,
            token_version=3,
            must_change_password=True,
            family_id=uuid4(),
        )

        decoded = decode_access_token(settings, token)

        assert decoded.subject == user_id
        assert decoded.realm == "tenant"
        assert decoded.tenant_id == tenant_id
        assert decoded.token_version == 3
        assert decoded.must_change_password is True
        assert decoded.jti == claims.jti

    def test_platform_token_has_no_tenant(self) -> None:
        """平台令牌不得携带 tenant_id。"""

        settings = _settings()
        token, _ = create_access_token(
            settings,
            user_id=uuid4(),
            realm="platform",
            tenant_id=None,
            token_version=0,
            must_change_password=False,
            family_id=uuid4(),
        )

        decoded = decode_access_token(settings, token)

        assert decoded.realm == "platform"
        assert decoded.tenant_id is None

    def test_realm_composition_is_enforced(self) -> None:
        """realm 与 tenant_id 组合非法时直接报错。"""

        settings = _settings()
        with pytest.raises(ValueError):
            create_access_token(
                settings,
                user_id=uuid4(),
                realm="tenant",
                tenant_id=None,
                token_version=0,
                must_change_password=False,
                family_id=uuid4(),
            )
        with pytest.raises(ValueError):
            create_access_token(
                settings,
                user_id=uuid4(),
                realm="platform",
                tenant_id=uuid4(),
                token_version=0,
                must_change_password=False,
                family_id=uuid4(),
            )

    def test_audience_mismatch_is_rejected(self) -> None:
        """跨 realm 混用（平台令牌改判为租户）必须失败（TD-074 的第一道防线）。"""

        settings = _settings()
        token, _ = create_access_token(
            settings,
            user_id=uuid4(),
            realm="platform",
            tenant_id=None,
            token_version=0,
            must_change_password=False,
            family_id=uuid4(),
        )

        # 用租户 audience 的配置解析平台令牌 → 拒绝
        tenant_only = _settings(
            jwt_audience_tenant=settings.jwt_audience_platform,
            jwt_audience_platform="other-platform-audience",
        )
        with pytest.raises(TokenError):
            decode_access_token(tenant_only, token)

    def test_expired_token_is_rejected(self) -> None:
        """过期令牌返回 TOKEN_EXPIRED。"""

        settings = _settings(access_token_ttl_seconds=1)
        token, _ = create_access_token(
            settings,
            user_id=uuid4(),
            realm="tenant",
            tenant_id=uuid4(),
            token_version=0,
            must_change_password=False,
            family_id=uuid4(),
            now=datetime.now(UTC) - timedelta(seconds=5),
        )

        with pytest.raises(TokenError) as excinfo:
            decode_access_token(settings, token)
        assert excinfo.value.code == "TOKEN_EXPIRED"

    def test_tampered_token_is_rejected(self) -> None:
        """签名不匹配（换密钥）返回 TOKEN_INVALID。"""

        settings = _settings()
        token, _ = create_access_token(
            settings,
            user_id=uuid4(),
            realm="tenant",
            tenant_id=uuid4(),
            token_version=0,
            must_change_password=False,
            family_id=uuid4(),
        )

        with pytest.raises(TokenError) as excinfo:
            decode_access_token(_settings(jwt_secret="another-secret-0123456789abcdefghij"), token)
        assert excinfo.value.code == "TOKEN_INVALID"


    @pytest.mark.parametrize("amr", ["pwd", {"totp": True}, 42, None])
    def test_malformed_amr_claim_degrades_to_empty(self, amr: object) -> None:
        """`amr` 不是字符串列表时按「未完成 MFA」解析：字符串不能被拆成字符、dict 不能被当成已验证。"""

        settings = _settings()
        now = datetime.now(UTC)
        forged = pyjwt.encode(
            {
                "sub": str(uuid4()),
                "realm": "tenant",
                "tenant_id": str(uuid4()),
                "jti": str(uuid4()),
                "sid": str(uuid4()),
                "token_version": 0,
                "must_change_password": False,
                "amr": amr,
                "iss": settings.jwt_issuer,
                "aud": settings.jwt_audience_tenant,
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(seconds=300)).timestamp()),
            },
            settings.jwt_secret.get_secret_value(),
            algorithm=settings.jwt_algorithm,
        )

        claims = decode_access_token(settings, forged)

        assert claims.amr == frozenset(), f"畸形 amr 被解析成了 {claims.amr!r}"
        assert claims.mfa_verified is False


class TestRefreshToken:
    """刷新令牌串与哈希。"""

    def test_refresh_token_is_random_and_hashed(self) -> None:
        """刷新令牌随机、哈希固定长度且不可逆。"""

        first = generate_refresh_token()
        second = generate_refresh_token()

        assert first != second
        digest = hash_refresh_token(first)
        assert len(digest) == 64
        assert first not in digest

    def test_fingerprint_is_optional(self) -> None:
        """UA/IP 哈希：缺失返回 None，存在则不可逆。"""

        assert hash_client_fingerprint(None) is None
        assert hash_client_fingerprint("") is None
        value = hash_client_fingerprint("1.2.3.4")
        assert value is not None and len(value) == 64 and "1.2.3.4" not in value


class TestMfaToken:
    """MFA 临时令牌：audience/purpose 隔离与 realm/tenant_id 组合约束。"""

    def test_challenge_token_roundtrip(self) -> None:
        """租户挑战令牌可解析，claim 完整且 purpose 命中期望。"""

        settings = _settings()
        tenant_id, user_id = uuid4(), uuid4()
        token, jti = create_mfa_token(
            settings,
            purpose="mfa_challenge",
            user_id=user_id,
            realm="tenant",
            tenant_id=tenant_id,
            token_version=2,
        )

        claims = decode_mfa_token(settings, token, expected_purpose="mfa_challenge")

        assert claims.subject == user_id
        assert claims.tenant_id == tenant_id
        assert claims.jti == jti
        assert claims.purpose == "mfa_challenge"

    def test_wrong_purpose_is_rejected(self) -> None:
        """挑战令牌不能当绑定令牌用（purpose 必须匹配）。"""

        settings = _settings()
        token, _ = create_mfa_token(
            settings,
            purpose="mfa_challenge",
            user_id=uuid4(),
            realm="tenant",
            tenant_id=uuid4(),
            token_version=0,
        )
        with pytest.raises(TokenError):
            decode_mfa_token(settings, token, expected_purpose="mfa_enroll")

    def test_platform_token_with_tenant_id_is_rejected(self) -> None:
        """伪造 realm=platform 却带 tenant_id 的令牌必须被拒（与签发约束对称）。"""

        settings = _settings()
        now = datetime.now(UTC)
        forged = pyjwt.encode(
            {
                "sub": str(uuid4()),
                "realm": "platform",
                "tenant_id": str(uuid4()),
                "purpose": "mfa_challenge",
                "jti": str(uuid4()),
                "token_version": 0,
                "iss": settings.jwt_issuer,
                "aud": settings.jwt_audience_mfa,
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(seconds=300)).timestamp()),
            },
            settings.jwt_secret.get_secret_value(),
            algorithm=settings.jwt_algorithm,
        )
        with pytest.raises(TokenError):
            decode_mfa_token(settings, forged, expected_purpose="mfa_challenge")

    def test_tenant_realm_requires_tenant_id(self) -> None:
        """租户 realm 铸造 MFA 令牌缺 tenant_id 必须拒发：发出去就是跨租可用的凭据。"""

        with pytest.raises(ValueError, match="租户 realm 的 MFA 令牌必须有 tenant_id"):
            create_mfa_token(
                _settings(),
                purpose="mfa_challenge",
                user_id=uuid4(),
                realm="tenant",
                tenant_id=None,
                token_version=0,
            )

    def test_platform_realm_rejects_tenant_id(self) -> None:
        """平台 realm 携带 tenant_id 必须拒发：两个 realm 的令牌命名空间互斥。"""

        with pytest.raises(ValueError, match="平台 realm 的 MFA 令牌不得携带 tenant_id"):
            create_mfa_token(
                _settings(),
                purpose="mfa_challenge",
                user_id=uuid4(),
                realm="platform",
                tenant_id=uuid4(),
                token_version=0,
            )
