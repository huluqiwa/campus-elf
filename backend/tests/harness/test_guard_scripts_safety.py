"""TD-363 分类化安全话术的必需点与危险负例。"""

from __future__ import annotations

import pytest

from app.harness.guards.scripts import safety_response_script
from app.harness.router import SafetyAction, SafetyCategory


@pytest.mark.parametrize(
    ("action", "category", "required"),
    (
        (
            SafetyAction.SUPPORT,
            SafetyCategory.SELF_HARM,
            ("关心", "可信成年人", "心理中心", "辅导员", "12356", "110", "120", "可执行"),
        ),
        (
            SafetyAction.REFUSE,
            SafetyCategory.SELF_HARM,
            ("无法", "自伤", "可信成年人", "心理中心", "12356", "110", "120"),
        ),
        (
            SafetyAction.SUPPORT,
            SafetyCategory.MENTAL_HEALTH,
            ("不能诊断", "专业评估", "心理中心", "医生", "紧急"),
        ),
        (
            SafetyAction.SUPPORT,
            SafetyCategory.VIOLENCE,
            ("理解", "安全", "可信", "辅导员", "保卫处", "证据", "110", "120", "报复"),
        ),
        (
            SafetyAction.REFUSE,
            SafetyCategory.VIOLENCE,
            ("拒绝", "法律", "校纪", "沟通", "求助", "110"),
        ),
        (
            SafetyAction.SUPPORT,
            SafetyCategory.ILLEGAL,
            ("安全", "证据", "可信成年人", "辅导员", "保卫处", "110", "报复"),
        ),
        (
            SafetyAction.REFUSE,
            SafetyCategory.ILLEGAL,
            (
                "拒绝",
                "步骤",
                "途径",
                "作弊",
                "考试纪律",
                "昏迷",
                "法律",
                "纪律处理",
                "保卫处",
                "110",
            ),
        ),
        (
            SafetyAction.SUPPORT,
            SafetyCategory.PRIVACY,
            ("停止", "密码", "截图", "辅导员", "信息保护", "110", "正式流程"),
        ),
        (
            SafetyAction.REFUSE,
            SafetyCategory.PRIVACY,
            (
                "未经授权",
                "信息",
                "正式业务渠道",
                "是否存在",
                "宿管",
                "辅导员",
                "保卫处",
                "私自搜查",
            ),
        ),
        (
            SafetyAction.SUPPORT,
            SafetyCategory.HARASSMENT,
            (
                "相信",
                "安全",
                "截图",
                "辅导员",
                "学生工作",
                "正式举报渠道",
                "纪检",
                "保卫处",
                "报警",
                "心理",
                "拒绝",
            ),
        ),
        (
            SafetyAction.REFUSE,
            SafetyCategory.HARASSMENT,
            ("拒绝", "骚扰", "跟踪", "法律", "校纪", "投诉渠道", "110"),
        ),
        (
            SafetyAction.REFUSE,
            SafetyCategory.ABUSIVE_CONTENT,
            ("拒绝", "辱骂", "羞辱", "理性沟通", "反馈", "调解"),
        ),
        (
            SafetyAction.SUPPORT,
            SafetyCategory.MAJOR_DECISION,
            ("重大决定", "不急于", "家长", "辅导员", "学业导师", "正式文件", "经办部门"),
        ),
    ),
)
def test_safety_script_has_required_points(
    action: SafetyAction,
    category: SafetyCategory,
    required: tuple[str, ...],
) -> None:
    script = safety_response_script(action, category)
    for point in required:
        assert point in script


def test_safety_scripts_do_not_claim_to_diagnose_or_promise_confidentiality() -> None:
    self_harm = safety_response_script(SafetyAction.SUPPORT, SafetyCategory.SELF_HARM)
    mental_health = safety_response_script(SafetyAction.SUPPORT, SafetyCategory.MENTAL_HEALTH)
    assert "你就是" not in mental_health
    assert "绝对保密" not in self_harm


def test_allow_none_has_no_safety_script() -> None:
    with pytest.raises(ValueError):
        safety_response_script(SafetyAction.ALLOW, SafetyCategory.NONE)


@pytest.mark.parametrize(
    ("action", "category"),
    (
        (SafetyAction.SUPPORT, SafetyCategory.ABUSIVE_CONTENT),
        (SafetyAction.REFUSE, SafetyCategory.MENTAL_HEALTH),
        (SafetyAction.REFUSE, SafetyCategory.MAJOR_DECISION),
    ),
)
def test_business_invalid_action_category_pair_rejected(
    action: SafetyAction, category: SafetyCategory
) -> None:
    with pytest.raises(ValueError, match="动作/类别组合"):
        safety_response_script(action, category)
