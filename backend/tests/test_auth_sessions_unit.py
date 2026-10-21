"""M1-C2 单元测试：设备标签、sid claim、会话上限配置与 Redis 降级。"""

from datetime import UTC, datetime
from uuid import uuid4

import jwt as pyjwt
import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError

from app.auth import revocation
from app.auth.sessions import device_label_from_user_agent, session_scope
from app.auth.tokens import (
    TokenError,
    create_access_token,
    decode_access_token,
)
from app.config import Settings

JWT_SECRET = "unit-test-jwt-secret-0123456789abcdef"


def test_tenant_session_scope_requires_tenant_id() -> None:
    """租户会话缺 tenant_id 必须 fail-closed，不得静默回退平台连接。"""

    with pytest.raises(ValueError, match="tenant_id"):
        session_scope(realm="tenant", tenant_id=None)


def _settings(**overrides: object) -> Settings:
    """构造测试配置（不读 .env）。"""

    base: dict[str, object] = {
        "_env_file": None,
        "redis_url": "redis://127.0.0.1:6379/15",
        "jwt_secret": JWT_SECRET,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestDeviceLabel:
    """UA 只解析为粗粒度标签，不保留原文。"""

    @pytest.mark.parametrize(
        ("user_agent", "expected"),
        [
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Chrome / Windows",
            ),
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
                "Edge / Windows",
            ),
            (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) "
                "Gecko/20100101 Firefox/120.0",
                "Firefox / macOS",
            ),
            (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1",
                "Safari / iOS",
            ),
            (
                "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
                "Chrome / Android",
            ),
            (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Chrome / Linux",
            ),
        ],
    )
    def test_known_user_agents(self, user_agent: str, expected: str) -> None:
        """常见浏览器/系统组合返回稳定标签。"""

        assert device_label_from_user_agent(user_agent) == expected

    @pytest.mark.parametrize("user_agent", [None, "", "curl/8.0", "UnknownBrowser"])
    def test_unknown_user_agents(self, user_agent: str | None) -> None:
        """无法识别时显示未知设备。"""

        assert device_label_from_user_agent(user_agent) == "未知设备"

    def test_label_length_is_bounded(self) -> None:
        """标签长度满足数据库列宽（64）。"""

        label = device_label_from_user_agent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        assert len(label) <= 64


class TestSessionClaim:
    """访问令牌必须携带 sid（= family_id）。"""

    def test_sid_roundtrip(self) -> None:
        """sid 签发后可解析，且与传入的 family_id 一致。"""

        settings = _settings()
        session_id = uuid4()
        token, claims = create_access_token(
            settings,
            user_id=uuid4(),
            realm="tenant",
            tenant_id=uuid4(),
            token_version=0,
            must_change_password=False,
            family_id=session_id,
        )

        decoded = decode_access_token(settings, token)

        assert claims.session_id == session_id
        assert decoded.session_id == session_id

    def test_missing_sid_is_rejected(self) -> None:
        """旧令牌/伪造令牌缺少 sid 时一律拒绝。"""

        settings = _settings()
        now = int(datetime.now(UTC).timestamp())
        payload = {
            "sub": str(uuid4()),
            "realm": "tenant",
            "tenant_id": str(uuid4()),
            "jti": str(uuid4()),
            "token_version": 0,
            "must_change_password": False,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience_tenant,
            "iat": now,
            "exp": now + 900,
        }
        token = pyjwt.encode(
            payload,
            settings.jwt_secret.get_secret_value(),
            algorithm=settings.jwt_algorithm,
            headers={"kid": settings.jwt_active_kid},
        )

        with pytest.raises(TokenError) as excinfo:
            decode_access_token(settings, token)
        assert excinfo.value.code == "TOKEN_INVALID"


class TestSessionLimitConfig:
    """设备上限必须为正整数且有安全的 realm 默认值。"""

    def test_defaults(self) -> None:
        """默认 C 端/校方 3、平台 2。"""

        settings = _settings()
        assert settings.max_sessions_per_user_tenant == 3
        assert settings.max_sessions_per_user_platform == 2

    @pytest.mark.parametrize("field", ["max_sessions_per_user_tenant", "max_sessions_per_user_platform"])
    def test_rejects_non_positive(self, field: str) -> None:
        """0/负数会让"上限"失去意义，启动即失败。"""

        with pytest.raises(ValidationError):
            _settings(**{field: 0})


class TestSessionRedisDegradation:
    """Redis 不可用时按简报 §6 降级：不因会话标记故障拒绝所有已鉴权请求。"""

    @pytest.mark.asyncio
    async def test_session_check_fails_open_when_redis_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MGET 连接失败时返回 True（仍由 token_version/账号状态兜底）。"""

        class _BrokenRedis:
            async def mget(self, *args: object, **kwargs: object) -> list[object]:
                raise RedisConnectionError("redis down")

        monkeypatch.setattr(revocation, "_redis", lambda: _BrokenRedis())

        assert await revocation.is_active(str(uuid4()), uuid4(), realm="tenant") is True
