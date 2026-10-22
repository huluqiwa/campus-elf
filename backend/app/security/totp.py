"""TOTP 生成与校验（RFC 6238：30 秒 / 6 位 / SHA-1）。

重放防护由调用方维护的水位 `last_used_step` 完成：只有严格大于已用步的时间步
才被接受，避免同一 30 秒窗口内重复使用同一个码。
"""

from __future__ import annotations

import hmac
import time

import pyotp

TOTP_SECRET_LENGTH = 32


def generate_totp_secret() -> str:
    """生成 160 位 Base32 随机 secret（不落库明文，由调用方加密）。"""

    return pyotp.random_base32(length=TOTP_SECRET_LENGTH)


def current_step(interval: int) -> int:
    """当前 TOTP 时间步（Unix 时间 / 步长）。"""

    return int(time.time() // interval)


def totp_uri(secret: str, *, issuer: str, account_name: str) -> str:
    """标准 otpauth:// URI，供前端渲染二维码。"""

    return pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name=issuer)


def verify_totp(
    secret: str,
    code: str,
    *,
    interval: int = 30,
    window_steps: int = 1,
    last_used_step: int | None = None,
    now_step: int | None = None,
) -> int | None:
    """校验 6 位码，成功返回匹配的时间步，失败返回 None。

    仅接受 `step > last_used_step` 的匹配（`None` 表示从未使用过）。
    """

    candidate = code.strip().replace(" ", "")
    if len(candidate) != 6 or not candidate.isdigit():
        return None
    reference = current_step(interval) if now_step is None else now_step
    totp = pyotp.TOTP(secret, interval=interval)
    for offset in range(-window_steps, window_steps + 1):
        step = reference + offset
        if step < 0:
            continue
        expected = totp.at(step * interval)
        if hmac.compare_digest(expected, candidate):
            if last_used_step is not None and step <= last_used_step:
                return None
            return step
    return None
