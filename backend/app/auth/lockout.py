"""登录失败窗口与账号锁定状态机（AUTH-05）。

规则：同一账号在窗口内连续失败达到阈值即锁定；锁定到期自动解锁；
成功登录/重置密码清零。状态由登录事务在每用户 advisory lock 内读改写，
本模块只负责纯时间计算，便于单元测试。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class LockoutState:
    """账号当前的失败计数与锁定状态。"""

    failed_attempts: int = 0
    window_started_at: datetime | None = None
    locked_until: datetime | None = None


def is_account_locked(locked_until: datetime | None, now: datetime) -> bool:
    """账号在 `now` 是否仍处于锁定中。"""

    return locked_until is not None and locked_until > now


def register_login_failure(
    state: LockoutState,
    *,
    now: datetime,
    max_failures: int,
    window_seconds: int,
    lockout_seconds: int,
) -> LockoutState:
    """登记一次认证失败并返回新状态。

    锁定期间重复失败不改写计数；锁定到期后从新窗口的 1 次开始，
    避免解锁后的第一次失败立即再次锁定。
    """

    if is_account_locked(state.locked_until, now):
        return state

    attempts = state.failed_attempts
    window_started_at = state.window_started_at
    if state.locked_until is not None and state.locked_until <= now:
        attempts = 0
        window_started_at = None

    if window_started_at is None or now - window_started_at >= timedelta(seconds=window_seconds):
        attempts = 1
        window_started_at = now
    else:
        attempts += 1

    locked_until = now + timedelta(seconds=lockout_seconds) if attempts >= max_failures else None
    return LockoutState(
        failed_attempts=attempts,
        window_started_at=window_started_at,
        locked_until=locked_until,
    )
