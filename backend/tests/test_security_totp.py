"""M1-D2 TOTP 校验（单元）：窗口、重放水位与输入格式。"""

import pyotp

from app.security.totp import current_step, generate_totp_secret, totp_uri, verify_totp

INTERVAL = 30


def _code(secret: str, step: int) -> str:
    return pyotp.TOTP(secret, interval=INTERVAL).at(step * INTERVAL)


def test_generated_secret_is_base32() -> None:
    """secret 为 32 位 Base32，可用于生成标准 URI。"""

    secret = generate_totp_secret()

    assert len(secret) == 32
    uri = totp_uri(secret, issuer="CampusCompanionElf", account_name="ops@example.com")
    assert uri.startswith("otpauth://totp/")
    assert "issuer=CampusCompanionElf" in uri


def test_verify_accepts_current_code() -> None:
    """当前时间步的 6 位码通过，并返回该步。"""

    secret = generate_totp_secret()
    step = current_step(INTERVAL)

    assert verify_totp(secret, _code(secret, step), interval=INTERVAL) == step


def test_verify_accepts_adjacent_step_with_window() -> None:
    """±1 步窗口容忍客户端时钟偏差；窗口为 0 时相邻步拒绝。"""

    secret = generate_totp_secret()
    step = current_step(INTERVAL)
    previous_code = _code(secret, step - 1)

    assert (
        verify_totp(secret, previous_code, interval=INTERVAL, window_steps=1, now_step=step)
        == step - 1
    )
    assert (
        verify_totp(secret, previous_code, interval=INTERVAL, window_steps=0, now_step=step) is None
    )


def test_verify_rejects_replayed_step() -> None:
    """同一时间步的码只能用一次：step <= last_used_step 时拒绝。"""

    secret = generate_totp_secret()
    step = current_step(INTERVAL)
    code = _code(secret, step)

    assert verify_totp(secret, code, interval=INTERVAL, last_used_step=step - 1) == step
    assert verify_totp(secret, code, interval=INTERVAL, last_used_step=step) is None
    assert verify_totp(secret, code, interval=INTERVAL, last_used_step=step + 1) is None


def test_verify_rejects_wrong_or_malformed_code() -> None:
    """错误码、非 6 位、非数字直接拒绝。"""

    secret = generate_totp_secret()
    step = current_step(INTERVAL)

    wrong = "000000" if _code(secret, step) != "000000" else "111111"
    assert verify_totp(secret, wrong, interval=INTERVAL) is None
    for malformed in ("", "12345", "1234567", "abcdef"):
        assert verify_totp(secret, malformed, interval=INTERVAL) is None
