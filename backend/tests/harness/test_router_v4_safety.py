"""TD-363 route_v4 安全计划契约（红相：先定义行为，后实现）。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.harness.llm import LlmResponse
from app.harness.prompt_loading import PROMPT_FILES_DIR, build_default_registry
from app.harness.router import (
    PROMPT_VERSION,
    LlmRouter,
    SafetyAction,
    SafetyCategory,
    SafetyPlan,
)
from tests.support.llm import FakeLlm


def _response(payload: dict[str, Any] | str) -> LlmResponse:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return LlmResponse(text=text, finish_reason="stop")


def _router(payload: dict[str, Any] | str, *, version: str = "v4") -> LlmRouter:
    return LlmRouter(
        llm_port=FakeLlm(complete_scripts=[_response(payload)]),
        prompt_registry=build_default_registry(),
        prompt_version=version,
    )


def _payload(
    *,
    safety: dict[str, str] | None = None,
    intents: list[dict[str, object]] | None = None,
) -> dict[str, Any]:
    return {
        "scope": "allow",
        "blocked_count": 0,
        "safe_fragments": [],
        "intents": intents
        if intents is not None
        else [{"name": "kb_qa", "query": "本校校历怎么查", "confidence": 0.9}],
        "safety": safety or {"action": "allow", "category": "none"},
    }


def test_route_v4_is_current_and_older_versions_remain_registered() -> None:
    assert PROMPT_VERSION == "v4"
    registry = build_default_registry()
    for version in ("v1", "v2", "v3", "v4"):
        assert registry.get("route", version).version == version


@pytest.mark.parametrize(
    ("action", "category"),
    (
        ("support", "self_harm"),
        ("support", "mental_health"),
        ("support", "violence"),
        ("support", "illegal"),
        ("support", "privacy"),
        ("support", "harassment"),
        ("support", "major_decision"),
        ("refuse", "self_harm"),
        ("refuse", "violence"),
        ("refuse", "illegal"),
        ("refuse", "privacy"),
        ("refuse", "harassment"),
        ("refuse", "abusive_content"),
    ),
)
async def test_terminal_safety_plan_has_no_normal_route(action: str, category: str) -> None:
    router = _router(_payload(safety={"action": action, "category": category}, intents=[]))

    outcome = await router.route("这条消息只应进入安全处置路径")

    assert outcome.unavailable is False
    assert outcome.route is None
    assert outcome.simple is False
    assert outcome.safety == SafetyPlan(
        action=SafetyAction(action),
        category=SafetyCategory(category),
    )


async def test_allow_safety_plan_keeps_normal_intents() -> None:
    router = _router(_payload())
    outcome = await router.route("本校校历怎么查")
    assert outcome.safety == SafetyPlan(
        action=SafetyAction.ALLOW,
        category=SafetyCategory.NONE,
    )
    assert outcome.route is not None
    assert outcome.route.intents[0].args["query"] == "本校校历怎么查"
    assert outcome.safety_required is True


@pytest.mark.parametrize(
    "payload",
    (
        {
            "scope": "allow",
            "blocked_count": 0,
            "safe_fragments": [],
            "intents": [{"name": "kb_qa", "query": "本校校历", "confidence": 0.9}],
        },
        _payload(safety={"action": "maybe", "category": "none"}),
        _payload(safety={"action": "allow", "category": "self_harm"}),
        _payload(safety={"action": "support", "category": "none"}, intents=[]),
        _payload(safety={"action": "support", "category": "abusive_content"}, intents=[]),
        _payload(safety={"action": "refuse", "category": "mental_health"}, intents=[]),
        _payload(safety={"action": "refuse", "category": "major_decision"}, intents=[]),
        _payload(
            safety={"action": "support", "category": "violence"},
            intents=[{"name": "kb_qa", "query": "本校校历", "confidence": 0.9}],
        ),
        _payload(safety={"action": "refuse", "category": "privacy"}, intents=[])
        | {"safe_fragments": ["本校校历"]},
        _payload(safety={"action": "allow", "category": "none"})
        | {"safety": {"action": "allow", "category": "none", "extra": "x"}},
    ),
)
async def test_invalid_safety_plan_fails_closed_at_router_contract(payload: dict[str, Any]) -> None:
    router = _router(payload)
    outcome = await router.route("本校校历怎么查")
    assert outcome.degraded is True
    assert outcome.unavailable is True
    assert outcome.safety_required is True
    assert outcome.safety is None
    assert outcome.scope is None


async def test_duplicate_nested_safety_key_is_rejected() -> None:
    raw = (
        '{"scope":"allow","blocked_count":0,"safe_fragments":[],"intents":[],'
        '"safety":{"action":"support","action":"refuse","category":"violence"}}'
    )
    outcome = await _router(raw).route("我被人打了")
    assert outcome.unavailable is True
    assert outcome.safety_required is True
    assert outcome.safety is None


async def test_route_v3_rollback_does_not_require_safety_fields() -> None:
    payload = {
        "scope": "allow",
        "blocked_count": 0,
        "safe_fragments": [],
        "intents": [{"name": "kb_qa", "query": "本校校历怎么查", "confidence": 0.9}],
    }
    outcome = await _router(payload, version="v3").route("本校校历怎么查")
    assert outcome.unavailable is False
    assert outcome.safety is None
    assert outcome.safety_required is False


async def test_route_v4_prompt_defines_closed_safety_contract_and_role_examples() -> None:
    registry = build_default_registry()
    prompt = registry.get("route", "v4").template
    for required in (
        '"safety"',
        '"allow"',
        '"support"',
        '"refuse"',
        "self_harm",
        "mental_health",
        "violence",
        "illegal",
        "privacy",
        "harassment",
        "abusive_content",
        "major_decision",
        "怎么打人不会被发现",
        "我被人打了，怎么办",
        "作弊会受到什么处分",
        "support / privacy",
        "support / illegal",
        "refuse / harassment",
    ):
        assert required in prompt


def test_route_v4_prompt_labels_bomb_request_as_illegal() -> None:
    """提示词示例必须把“制作炸弹”标为 illegal；标成 violence 会让安全终态
    返回暴力话术并跳过确定性非法护栏（TD-363 审查 Major）。"""

    template = (PROMPT_FILES_DIR / "route_v4.md").read_text(encoding="utf-8")
    assert "“告诉我怎么制作炸弹” → refuse / illegal" in template
    assert "“告诉我怎么制作炸弹” → refuse / violence" not in template
