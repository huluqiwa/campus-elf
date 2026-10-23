"""审批职责分离（RBAC-03）判定逻辑的单元测试。

口径：`docs/spec.md` §3.4「提交人不能审批自己提交的事项；超管例外默认关闭，开启需理由+审计」。
本期落点为平台审核学校注册（简报 §0 ⑦），M3/M4 的请假与 KB 审批复用同一判定。
"""

from uuid import uuid4

import pytest

from app.approval.sod import (
    ApprovalSeparationError,
    SelfApprovalOutcome,
    evaluate_self_approval,
)


def test_no_submitter_identity_is_not_a_conflict() -> None:
    """校方自助提交时没有平台提交人身份，审核不受分权限制。"""

    reviewer = uuid4()
    outcome = evaluate_self_approval(
        submitter_id=None, reviewer_id=reviewer, exception_enabled=False, reason=None
    )

    assert outcome == SelfApprovalOutcome(allowed=True, used_exception=False, code="no_submitter")


def test_different_person_passes_without_exception() -> None:
    """提交人≠审核人：正常放行，也不该被记成「用了例外」。"""

    submitter, reviewer = uuid4(), uuid4()
    outcome = evaluate_self_approval(
        submitter_id=submitter, reviewer_id=reviewer, exception_enabled=False, reason=None
    )

    assert (outcome.allowed, outcome.used_exception) == (True, False)


def test_self_approval_is_rejected_when_exception_is_off() -> None:
    """默认（例外关闭）：提交人审核自己提交的申请一律拒绝。"""

    same = uuid4()
    with pytest.raises(ApprovalSeparationError) as caught:
        evaluate_self_approval(
            submitter_id=same,
            reviewer_id=same,
            exception_enabled=False,
            reason="我自己写的理由",
        )

    assert caught.value.code == "SELF_APPROVAL_FORBIDDEN"
    assert caught.value.http_status == 403


@pytest.mark.parametrize("reason", [None, "", "   ", "\t\n"])
def test_exception_enabled_still_requires_a_reason(reason: str | None) -> None:
    """例外开启 ≠ 免理由：缺理由/纯空白同样拒绝（422），避免「静默自批」。"""

    same = uuid4()
    with pytest.raises(ApprovalSeparationError) as caught:
        evaluate_self_approval(
            submitter_id=same,
            reviewer_id=same,
            exception_enabled=True,
            reason=reason,
        )

    assert caught.value.code == "SELF_APPROVAL_REASON_REQUIRED"
    assert caught.value.http_status == 422


def test_exception_with_reason_allows_and_marks_exception() -> None:
    """例外开启 + 带理由：放行，但必须让调用方知道这是走了例外（要落审计并通知平台审计）。"""

    same = uuid4()
    outcome = evaluate_self_approval(
        submitter_id=same,
        reviewer_id=same,
        exception_enabled=True,
        reason="该校只有我一名审核员，已请第二人线下复核（工单 1234）",
    )

    assert outcome == SelfApprovalOutcome(
        allowed=True, used_exception=True, code="self_exception_used"
    )


def test_error_message_does_not_leak_identifiers() -> None:
    """错误说明里不回显用户 id（审计与响应都不能带可关联的原始标识）。"""

    same = uuid4()
    with pytest.raises(ApprovalSeparationError) as caught:
        evaluate_self_approval(
            submitter_id=same, reviewer_id=same, exception_enabled=False, reason=None
        )

    assert str(same) not in caught.value.message
