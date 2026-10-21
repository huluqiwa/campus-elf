"""M1-D1 单元测试：密码策略/历史、失败锁定状态机、重置令牌格式。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from redis.exceptions import RedisError

from app.auth import revocation
from app.auth.lockout import (
    LockoutState,
    is_account_locked,
    register_login_failure,
)
from app.auth.passwords import (
    PasswordPolicyError,
    generate_initial_password,
    hash_password,
    is_password_reused,
    validate_password_strength,
)
from app.auth.tokens import (
    TokenError,
    generate_password_reset_token,
    hash_password_reset_token,
    parse_password_reset_token,
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def test_password_policy_accepts_three_character_classes() -> None:
    """10 位以上且覆盖 3 类字符的口令通过策略。"""

    validate_password_strength("Abcdefghij1")
    validate_password_strength("Abcdefghij!")


@pytest.mark.parametrize(
    "password",
    [
        "Abcdefgh1",  # 9 位
        "abcdefghij",  # 仅小写
        "abcdefghij1",  # 仅小写 + 数字（2 类）
        "Abcdefghij",  # 仅大小写（2 类）
    ],
)
def test_password_policy_rejects_weak_passwords(password: str) -> None:
    """长度不足或字符类不足时拒绝。"""

    with pytest.raises(PasswordPolicyError):
        validate_password_strength(password)


def test_password_policy_rejects_bcrypt_byte_limit() -> None:
    """UTF-8 超过 72 字节必须拒绝（bcrypt 输入上限）。"""

    validate_password_strength("Aa1!" + "汉" * 22)  # 70 字节
    with pytest.raises(PasswordPolicyError, match="72"):
        validate_password_strength("Aa1!" + "汉" * 23)  # 73 字节
    with pytest.raises(PasswordPolicyError, match="72"):
        validate_password_strength("Aa1!" + "x" * 69)  # 73 字节


def test_password_policy_rejects_login_id_contained_in_password() -> None:
    """新口令不得包含学号/工号（大小写不敏感）。"""

    with pytest.raises(PasswordPolicyError, match="账号"):
        validate_password_strength("S-1001-Secure-9", login_id="s-1001")
    with pytest.raises(PasswordPolicyError, match="账号"):
        validate_password_strength("OpsFull-Pass-9", login_id="opsfull")


def test_password_history_detects_reuse() -> None:
    """最近历史里的旧口令一律判定为复用。"""

    old_hash = hash_password("Old-Pass-123")
    history = [hash_password("Older-Pass-88")]

    assert is_password_reused("Old-Pass-123", [old_hash, *history]) is True
    assert is_password_reused("Brand-New-Pass-9", [old_hash, *history]) is False


def test_initial_password_is_compliant_and_avoids_login_id() -> None:
    """随机初始密码满足策略，且不会包含登录名。"""

    password = generate_initial_password(avoid="admin")
    assert len(password) == 12
    validate_password_strength(password, login_id=None)
    assert "admin" not in password.lower()


def test_lockout_state_machine_locks_after_threshold() -> None:
    """窗口内第 5 次失败触发锁定；锁定期间重复输入不改写状态。"""

    state = LockoutState()
    for attempt in range(1, 5):
        state = register_login_failure(
            state,
            now=NOW + timedelta(seconds=attempt),
            max_failures=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        assert state.locked_until is None
        assert state.failed_attempts == attempt

    state = register_login_failure(
        state,
        now=NOW + timedelta(seconds=5),
        max_failures=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert state.failed_attempts == 5
    assert state.locked_until == NOW + timedelta(seconds=5, minutes=15)
    assert is_account_locked(state.locked_until, NOW + timedelta(minutes=1)) is True

    locked = register_login_failure(
        state,
        now=NOW + timedelta(minutes=2),
        max_failures=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert locked == state


def test_lockout_window_expiry_starts_fresh_counter() -> None:
    """窗口过期后失败计数从 1 重新开始。"""

    state = LockoutState(
        failed_attempts=4,
        window_started_at=NOW,
        locked_until=None,
    )
    refreshed = register_login_failure(
        state,
        now=NOW + timedelta(seconds=901),
        max_failures=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert refreshed.failed_attempts == 1
    assert refreshed.window_started_at == NOW + timedelta(seconds=901)
    assert refreshed.locked_until is None


def test_lockout_expiry_resets_counter_before_next_failure() -> None:
    """锁定到期后的下一次失败不会立即再次锁定。"""

    state = LockoutState(
        failed_attempts=5,
        window_started_at=NOW,
        locked_until=NOW + timedelta(minutes=15),
    )
    assert is_account_locked(state.locked_until, NOW + timedelta(minutes=14)) is True
    assert is_account_locked(state.locked_until, NOW + timedelta(minutes=16)) is False

    after_expiry = register_login_failure(
        state,
        now=NOW + timedelta(minutes=16),
        max_failures=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert after_expiry.failed_attempts == 1
    assert after_expiry.locked_until is None


def test_password_reset_token_tenant_format_and_hash() -> None:
    """租户重置令牌携带 tenant_id，数据库哈希只覆盖随机 secret。"""

    tenant_id = uuid4()
    token = generate_password_reset_token("tenant", tenant_id)
    assert token.startswith(f"v1rt.{tenant_id}.")

    realm, parsed_tenant, secret = parse_password_reset_token(token)
    assert realm == "tenant"
    assert parsed_tenant == tenant_id
    assert secret
    assert token.split(".", 2)[2] == secret

    digest = hash_password_reset_token(secret)
    assert len(digest) == 64
    int(digest, 16)
    assert digest == hash_password_reset_token(secret)


def test_password_reset_token_platform_format_and_malformed_rejected() -> None:
    """平台重置令牌不含 tenant_id；格式非法一律拒绝。"""

    token = generate_password_reset_token("platform", None)
    assert token.startswith("v1rp.")
    realm, parsed_tenant, secret = parse_password_reset_token(token)
    assert realm == "platform"
    assert parsed_tenant is None
    assert secret

    malformed = [
        "",
        "v1rt",
        f"v1rt.{uuid4()}",
        f"v1rt.{uuid4()}.{uuid4()}.",
        f"v1rt.not-a-uuid.{'x' * 16}",
        f"v1rp.{'x' * 16}",  # 平台令牌必须恰好两段
        "v2rp.abc",
    ]
    for value in malformed:
        with pytest.raises(TokenError):
            parse_password_reset_token(value)


@pytest.mark.asyncio
async def test_rate_limit_fails_open_when_redis_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis 故障时重置申请限流放行（由冷却/审计兜底），不能变成 500。"""

    class _BrokenRedis:
        def pipeline(self, transaction: bool = True) -> None:
            raise RedisError("redis down")

    monkeypatch.setattr(revocation, "_redis", lambda: _BrokenRedis())

    assert (
        await revocation.allow_rate_limit("password_reset", "ip-hash", limit=1, window_seconds=60)
        is True
    )
