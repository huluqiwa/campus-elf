"""LLM 熔断器状态机校验（M4-AI01a 步 5）。

A8 口径：窗口 60s 内 5 次失败 → 开 30s → 半开试探 1 次。全部用注入时钟驱动，
不 sleep 真实时间；并发试探约束（同时只放行 1 次）用同步两次 before_call 断言。
"""

import pytest

from app.harness.llm import LlmCircuitBreaker, LlmError, LlmErrorCode


class _FakeClock:
    """可推进的单调时钟（替代 time.monotonic）。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def breaker(clock: _FakeClock, *, threshold: int = 5) -> LlmCircuitBreaker:
    """按默认 A8 参数构造（阈值可覆盖）。"""

    return LlmCircuitBreaker(
        failure_threshold=threshold,
        window_seconds=60.0,
        open_seconds=30.0,
        clock=clock,
    )


def test_fresh_breaker_allows_calls() -> None:
    """新熔断器闭合，放行调用。"""

    clock = _FakeClock()
    cb = breaker(clock)
    cb.before_call()
    assert cb.state == "closed"


def test_failures_below_threshold_stay_closed() -> None:
    """窗口内失败数未达阈值保持闭合。"""

    clock = _FakeClock()
    cb = breaker(clock)
    for _ in range(4):
        cb.record_failure()
    cb.before_call()
    assert cb.state == "closed"


def test_threshold_failures_open_circuit() -> None:
    """窗口内第 5 次失败后开路：before_call 抛 BREAKER_OPEN 且可重试标记为 True。"""

    clock = _FakeClock()
    cb = breaker(clock)
    for _ in range(5):
        cb.record_failure()
    assert cb.state == "open"
    with pytest.raises(LlmError) as excinfo:
        cb.before_call()
    assert excinfo.value.code is LlmErrorCode.BREAKER_OPEN
    assert excinfo.value.retryable is True


def test_failures_outside_window_expire() -> None:
    """窗口滑出：旧失败不计入，不误开路。"""

    clock = _FakeClock()
    cb = breaker(clock)
    for _ in range(3):
        cb.record_failure()
    clock.advance(61.0)
    for _ in range(3):
        cb.record_failure()
    cb.before_call()
    assert cb.state == "closed"


def test_open_circuit_half_opens_after_open_seconds() -> None:
    """开路期拒绝；开路时长过后放行一次试探（半开）。"""

    clock = _FakeClock()
    cb = breaker(clock)
    for _ in range(5):
        cb.record_failure()
    clock.advance(29.0)
    with pytest.raises(LlmError):
        cb.before_call()
    clock.advance(1.0)
    cb.before_call()
    assert cb.state == "half_open"


def test_half_open_trial_is_single_flight() -> None:
    """半开只放行一次试探：试探未记结果前再次 before_call 被拒。"""

    clock = _FakeClock()
    cb = breaker(clock)
    for _ in range(5):
        cb.record_failure()
    clock.advance(30.0)
    cb.before_call()
    with pytest.raises(LlmError) as excinfo:
        cb.before_call()
    assert excinfo.value.code is LlmErrorCode.BREAKER_OPEN


def test_half_open_success_resets_to_closed() -> None:
    """半开试探成功 → 闭合且历史失败清零（新一轮计数从空开始）。"""

    clock = _FakeClock()
    cb = breaker(clock)
    for _ in range(5):
        cb.record_failure()
    clock.advance(30.0)
    cb.before_call()
    cb.record_success()
    assert cb.state == "closed"
    # 清零验证：再失败 4 次不应开路（若未清零则第 4 次即第 9 次窗口内失败）
    for _ in range(4):
        cb.record_failure()
    cb.before_call()
    assert cb.state == "closed"


def test_half_open_failure_reopens_with_fresh_window() -> None:
    """半开试探失败 → 重新开路，且开路窗口从失败时刻重新计起。"""

    clock = _FakeClock()
    cb = breaker(clock)
    for _ in range(5):
        cb.record_failure()
    clock.advance(30.0)
    cb.before_call()
    cb.record_failure()
    assert cb.state == "open"
    clock.advance(15.0)
    with pytest.raises(LlmError):
        cb.before_call()
    clock.advance(15.0)
    cb.before_call()
    state: str = cb.state  # 注解为 str：断开 mypy 对属性的前序收窄（comparison-overlap）
    assert state == "half_open"


def test_success_resets_failure_window() -> None:
    """闭合态下成功即清空失败计数（偶发失败不累积成开路）。"""

    clock = _FakeClock()
    cb = breaker(clock)
    for _ in range(3):
        cb.record_failure()
    cb.record_success()
    for _ in range(3):
        cb.record_failure()
    cb.before_call()
    assert cb.state == "closed"


def test_release_trial_recovers_slot_and_keeps_open() -> None:
    """放弃试探（取消/中断）：名额回收、开路保持、可再次授予试探（审查 M1）。"""

    clock = _FakeClock()
    cb = breaker(clock)
    for _ in range(5):
        cb.record_failure()
    clock.advance(30.0)
    cb.before_call()
    cb.release_trial()
    assert cb.state == "open"
    cb.before_call()  # 名额已回收：再次授予不抛出
    state: str = cb.state  # 注解为 str：断开 mypy 属性收窄（comparison-overlap）
    assert state == "half_open"


def test_release_trial_does_not_count_as_failure() -> None:
    """放弃试探不计失败：释放后成功仍可闭合（失败计数未因放弃而累积）。"""

    clock = _FakeClock()
    cb = breaker(clock)
    for _ in range(5):
        cb.record_failure()
    clock.advance(30.0)
    cb.before_call()
    cb.release_trial()
    cb.before_call()
    cb.record_success()
    assert cb.state == "closed"
    # 失败计数已清零：再失败 4 次不开路（若放弃被记成失败，此处会提前开路）
    for _ in range(4):
        cb.record_failure()
    cb.before_call()
    assert cb.state == "closed"


def test_release_trial_without_pending_trial_is_noop() -> None:
    """闭合/无试探在飞时 release_trial 是安全的空操作（防御幂等）。"""

    clock = _FakeClock()
    cb = breaker(clock)
    cb.release_trial()
    cb.before_call()
    assert cb.state == "closed"
