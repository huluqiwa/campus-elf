"""KB 状态机单测（UPLOADED 的两条扫描迁移 + 可提交谓词 + KB-02 审批结论与构建门禁）。"""

import pytest

from app.kb.status import (
    KB_STATUS_APPROVED,
    KB_STATUS_PENDING_APPROVAL,
    KB_STATUS_REJECTED,
    KB_STATUS_SCAN_FAILED,
    KB_STATUS_UPLOADED,
    InvalidStatusTransition,
    NotBuildableError,
    NotSubmittableError,
    assert_build_allowed,
    assert_submittable,
    assert_transition,
    can_transition,
)


def test_uploaded_can_only_go_to_scan_outcomes() -> None:
    """UPLOADED 只允许迁移到 PENDING_APPROVAL 或 SCAN_FAILED。"""

    assert can_transition(KB_STATUS_UPLOADED, KB_STATUS_PENDING_APPROVAL) is True
    assert can_transition(KB_STATUS_UPLOADED, KB_STATUS_SCAN_FAILED) is True
    assert can_transition(KB_STATUS_UPLOADED, KB_STATUS_UPLOADED) is False
    assert can_transition(KB_STATUS_UPLOADED, "APPROVED") is False


def test_pending_approval_can_only_go_to_decision() -> None:
    """PENDING_APPROVAL 只允许迁移到 APPROVED 或 REJECTED（本点无出边再到别处）。"""

    assert can_transition(KB_STATUS_PENDING_APPROVAL, KB_STATUS_APPROVED) is True
    assert can_transition(KB_STATUS_PENDING_APPROVAL, KB_STATUS_REJECTED) is True
    assert can_transition(KB_STATUS_PENDING_APPROVAL, KB_STATUS_PENDING_APPROVAL) is False
    assert can_transition(KB_STATUS_PENDING_APPROVAL, KB_STATUS_UPLOADED) is False


def test_decision_states_reject_all_transitions() -> None:
    """本点 APPROVED / REJECTED / SCAN_FAILED / 未知状态都是终态：一律拒绝迁移。"""

    for current in (KB_STATUS_APPROVED, KB_STATUS_REJECTED, KB_STATUS_SCAN_FAILED, "UNKNOWN"):
        for target in (
            KB_STATUS_UPLOADED,
            KB_STATUS_PENDING_APPROVAL,
            KB_STATUS_APPROVED,
            KB_STATUS_REJECTED,
        ):
            assert can_transition(current, target) is False


def test_assert_transition_raises_with_both_states() -> None:
    """非法迁移抛出可定位的异常（供 409/日志使用）。"""

    assert_transition(KB_STATUS_UPLOADED, KB_STATUS_PENDING_APPROVAL)

    with pytest.raises(InvalidStatusTransition, match=r"PENDING_APPROVAL.*UPLOADED"):
        assert_transition(KB_STATUS_PENDING_APPROVAL, KB_STATUS_UPLOADED)


def test_only_pending_approval_is_submittable() -> None:
    """未扫描/扫描失败都不可提交审批（扫描未通过绝不进审批）。"""

    assert_submittable(KB_STATUS_PENDING_APPROVAL)

    for status in (KB_STATUS_UPLOADED, KB_STATUS_SCAN_FAILED, "UNKNOWN"):
        with pytest.raises(NotSubmittableError, match="不可提交审批"):
            assert_submittable(status)


def test_build_only_allowed_from_approved() -> None:
    """构建门禁：只有 APPROVED 可创建构建任务（KB-02 的 409 依据）。"""

    assert_build_allowed(KB_STATUS_APPROVED)

    for status in (
        KB_STATUS_UPLOADED,
        KB_STATUS_PENDING_APPROVAL,
        KB_STATUS_SCAN_FAILED,
        KB_STATUS_REJECTED,
        "UNKNOWN",
    ):
        with pytest.raises(NotBuildableError, match="不可创建构建任务"):
            assert_build_allowed(status)
