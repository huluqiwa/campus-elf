"""TD-363：sensitive_content 语料的**服务端固定话术模板**矩阵。

注意：本文件用下方 ``_PLANS`` 里**手写**的安全计划驱动 ``safety_response_script``，
验证的是服务端模板对 20 条语料的覆盖与脱敏，**不验证模型分类是否正确**。模型把
用户消息判成 support/refuse 与哪个 category，属于 route_v4 的语义分类能力，由
evals 队列单独评估（TD-362/365），不能用本文件冒充。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.harness.guards.scripts import safety_response_script
from app.harness.router import SafetyAction, SafetyCategory, SafetyPlan

_CASES_PATH = Path(__file__).resolve().parents[3] / "evals" / "cases" / "sensitive_content.jsonl"
_PLANS = {
    "sensitive_content-001": SafetyPlan(SafetyAction.SUPPORT, SafetyCategory.SELF_HARM),
    "sensitive_content-002": SafetyPlan(SafetyAction.SUPPORT, SafetyCategory.MENTAL_HEALTH),
    "sensitive_content-003": SafetyPlan(SafetyAction.SUPPORT, SafetyCategory.VIOLENCE),
    "sensitive_content-004": SafetyPlan(SafetyAction.REFUSE, SafetyCategory.VIOLENCE),
    "sensitive_content-005": SafetyPlan(SafetyAction.REFUSE, SafetyCategory.PRIVACY),
    "sensitive_content-006": SafetyPlan(SafetyAction.REFUSE, SafetyCategory.VIOLENCE),
    "sensitive_content-007": SafetyPlan(SafetyAction.REFUSE, SafetyCategory.ILLEGAL),
    "sensitive_content-008": SafetyPlan(SafetyAction.SUPPORT, SafetyCategory.VIOLENCE),
    "sensitive_content-009": SafetyPlan(SafetyAction.SUPPORT, SafetyCategory.MAJOR_DECISION),
    "sensitive_content-010": SafetyPlan(SafetyAction.REFUSE, SafetyCategory.PRIVACY),
    "sensitive_content-011": SafetyPlan(SafetyAction.REFUSE, SafetyCategory.ABUSIVE_CONTENT),
    "sensitive_content-012": SafetyPlan(SafetyAction.REFUSE, SafetyCategory.ILLEGAL),
    "sensitive_content-013": SafetyPlan(SafetyAction.SUPPORT, SafetyCategory.SELF_HARM),
    "sensitive_content-014": SafetyPlan(SafetyAction.SUPPORT, SafetyCategory.SELF_HARM),
    "sensitive_content-015": SafetyPlan(SafetyAction.REFUSE, SafetyCategory.ILLEGAL),
    "sensitive_content-016": SafetyPlan(SafetyAction.REFUSE, SafetyCategory.ILLEGAL),
    "sensitive_content-017": SafetyPlan(SafetyAction.SUPPORT, SafetyCategory.HARASSMENT),
    "sensitive_content-018": SafetyPlan(SafetyAction.REFUSE, SafetyCategory.PRIVACY),
    "sensitive_content-019": SafetyPlan(SafetyAction.REFUSE, SafetyCategory.ILLEGAL),
    "sensitive_content-020": SafetyPlan(SafetyAction.SUPPORT, SafetyCategory.SELF_HARM),
}


def _cases() -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in _CASES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_matrix_covers_exact_original_sensitive_corpus() -> None:
    ids = {str(case["id"]) for case in _cases()}
    assert len(ids) == 20
    assert ids == set(_PLANS)


@pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["id"]))
def test_every_sensitive_case_has_a_terminal_server_script(
    case: dict[str, object],
) -> None:
    plan = _PLANS[str(case["id"])]
    script = safety_response_script(plan.action, plan.category)
    assert script
    if plan.action is SafetyAction.SUPPORT:
        assert any(marker in script for marker in ("安全", "支持", "帮助", "求助"))
    if plan.action is SafetyAction.REFUSE:
        assert any(marker in script for marker in ("拒绝", "无法"))
    assert "用户消息：" not in script
    assert str(case["input"]) not in script


@pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["id"]))
def test_every_sensitive_script_avoids_its_case_literal_forbidden_terms(
    case: dict[str, object],
) -> None:
    plan = _PLANS[str(case["id"])]
    script = safety_response_script(plan.action, plan.category)
    forbidden = case.get("forbidden", [])
    assert isinstance(forbidden, list)
    for forbidden_point in forbidden:
        assert str(forbidden_point) not in script


def test_014_self_harm_script_also_covers_network_abuse_evidence_path() -> None:
    script = safety_response_script(SafetyAction.SUPPORT, SafetyCategory.SELF_HARM)
    assert "网暴" in script
    assert "证据" in script
