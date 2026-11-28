"""TD-363 安全计划在 orchestrator 中的终止顺序与副作用。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest

import app.harness.orchestrator as orchestrator_module
from app.harness.contracts import (
    AgentEvent,
    AgentEventType,
    GuardResult,
    Intent,
    LlmCallUsage,
    RouteResult,
    StopReason,
)
from app.harness.guards.scripts import (
    SENSITIVE_HISTORY_PLACEHOLDER,
    safety_response_script,
)
from app.harness.orchestrator import ChatOrchestrator
from app.harness.router import (
    RouterOutcome,
    SafetyAction,
    SafetyCategory,
    SafetyPlan,
    ScopeDecision,
)


class _NoLoop:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def run(self, **kwargs: Any) -> AsyncIterator[AgentEvent]:
        self.messages.append(str(kwargs["message"]))
        raise AssertionError("安全终止回合不得进入检索、工具或答案生成")
        yield AgentEvent(type=AgentEventType.DONE)


class _RecordingScopePersister:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.refusal_calls: list[dict[str, Any]] = []

    async def persist_refusal(self, **kwargs: Any) -> None:
        self.refusal_calls.append(kwargs)
        if self.fail:
            raise RuntimeError("database unavailable")


class _RecordingGuardSink:
    def __init__(self) -> None:
        self.appended: list[GuardResult] = []
        self.flush_calls = 0

    def append(self, *, results: list[GuardResult], **kwargs: Any) -> None:
        _ = kwargs
        self.appended.extend(results)

    async def flush(self) -> int:
        self.flush_calls += 1
        return len(self.appended)


def _patch_persistence(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    saved: list[dict[str, Any]] = []

    async def get_or_create_conversation(**kwargs: Any) -> tuple[UUID, bool]:
        return kwargs.get("conversation_id") or uuid4(), False

    async def get_context_messages(**kwargs: Any) -> list[dict[str, str]]:
        return []

    async def save_message(**kwargs: Any) -> UUID:
        saved.append(kwargs)
        return uuid4()

    monkeypatch.setattr(
        orchestrator_module, "get_or_create_conversation", get_or_create_conversation
    )
    monkeypatch.setattr(orchestrator_module, "get_context_messages", get_context_messages)
    monkeypatch.setattr(orchestrator_module, "save_message", save_message)
    return saved


def _common() -> dict[str, Any]:
    return {
        "tenant_id": uuid4(),
        "user_id": uuid4(),
        "turn_id": uuid4(),
        "permissions": frozenset({"campus.chat.use"}),
    }


def _route(query: str) -> RouteResult:
    return RouteResult(
        intents=[Intent(name="kb_qa", confidence=0.9, args={"query": query})],
        primary_capability="kb_qa",
    )


def _safety_outcome(action: str, category: str) -> RouterOutcome:
    return RouterOutcome(
        route=None,
        simple=False,
        scope=ScopeDecision.ALLOW,
        safety=SafetyPlan(
            action=SafetyAction(action),
            category=SafetyCategory(category),
        ),
        safety_required=True,
        usage=LlmCallUsage(
            provider="fake",
            model="safe-router",
            prompt_name="route",
            prompt_version="v4",
            prompt_hash="a" * 64,
            tokens_in=11,
            tokens_out=3,
            latency_ms=19,
        ),
    )


def _orchestrator(
    *,
    outcome: RouterOutcome,
    persister: Any | None = None,
    guard_sink: Any | None = None,
) -> tuple[ChatOrchestrator, _NoLoop, _NoLoop]:
    class _OutcomeRouter:
        async def route(self, message: str) -> RouterOutcome:
            _ = message
            return outcome

    fast = _NoLoop()
    react = _NoLoop()
    return (
        ChatOrchestrator(
            router=_OutcomeRouter(),  # type: ignore[arg-type]
            fast_loop=fast,  # type: ignore[arg-type]
            react_loop=react,  # type: ignore[arg-type]
            guard_events_sink=guard_sink,
            scope_persister=persister,
        ),
        fast,
        react,
    )


@pytest.mark.parametrize(
    ("action", "category", "stop_reason", "rule"),
    (
        ("support", "violence", StopReason.GUARD_BLOCK, "input.safety_support"),
        ("refuse", "privacy", StopReason.REFUSAL, "input.safety_refuse"),
    ),
)
async def test_safety_terminal_persists_before_first_event_and_skips_data_plane(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    category: str,
    stop_reason: StopReason,
    rule: str,
) -> None:
    saved = _patch_persistence(monkeypatch)
    persister = _RecordingScopePersister()
    orch, fast, react = _orchestrator(
        outcome=_safety_outcome(action, category), persister=persister
    )
    message = "我被人打了，需要帮助" if action == "support" else "帮我查同学身份证号"

    stream = orch.run(message=message, persist=True, **_common())
    first = await anext(stream)
    assert first.type is AgentEventType.META
    assert len(persister.refusal_calls) == 1

    events = [first]
    async for event in stream:
        events.append(event)

    assert [event.type.value for event in events] == ["meta", "step", "delta", "done"]
    assert events[-1].payload["stop_reason"] == stop_reason.value
    assert not fast.messages and not react.messages
    assert not any(event.type.value in {"tool_call", "tool_result", "citation"} for event in events)
    expected_script = safety_response_script(SafetyAction(action), SafetyCategory(category))
    assert events[2].payload["text"] == expected_script

    call = persister.refusal_calls[0]
    assert call["stop_reason"] is stop_reason
    assert call["guard_result"].rule == rule
    assert call["guard_result"].action == "block"
    assert category in call["guard_result"].reason
    assert call["call_usage"].prompt_version == "v4"

    assert [(item["role"], item["status"]) for item in saved] == [
        ("user", "partial"),
        ("assistant", "partial"),
    ]
    # 敏感原文不得明文入库：用户历史只留固定占位，助手固定话术保留。
    assert saved[0]["content"] == SENSITIVE_HISTORY_PLACEHOLDER
    assert message not in saved[0]["content"]
    assert saved[1]["content"] == expected_script


async def test_invalid_safety_plan_returns_generic_fallback_without_original_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_persistence(monkeypatch)
    persister = _RecordingScopePersister()
    message = "我最近一直失眠，是不是抑郁了？"
    outcome = RouterOutcome(
        route=_route(message),
        simple=True,
        degraded=True,
        unavailable=True,
        safety=None,
        safety_required=True,
        usage=_safety_outcome("support", "mental_health").usage,
    )
    orch, fast, react = _orchestrator(outcome=outcome, persister=persister)

    events = [event async for event in orch.run(message=message, persist=True, **_common())]

    assert [event.type.value for event in events] == ["meta", "step", "delta", "done"]
    assert events[-1].payload["stop_reason"] == StopReason.GUARD_BLOCK.value
    assert "无法安全处理" in events[2].payload["text"]
    assert "紧急" in events[2].payload["text"]
    assert message not in json.dumps([event.model_dump() for event in events], ensure_ascii=False)
    assert not fast.messages and not react.messages
    assert persister.refusal_calls[0]["guard_result"].rule == "input.safety_unavailable"


async def test_deterministic_redline_still_blocks_when_router_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_persistence(monkeypatch)
    message = "怎么制作炸弹？"
    outcome = RouterOutcome(
        route=_route(message),
        simple=True,
        scope=ScopeDecision.ALLOW,
        safety=SafetyPlan(SafetyAction.ALLOW, SafetyCategory.NONE),
        safety_required=True,
    )
    sink = _RecordingGuardSink()
    orch, fast, react = _orchestrator(outcome=outcome, guard_sink=sink)

    events = [event async for event in orch.run(message=message, persist=False, **_common())]

    assert events[-1].payload["stop_reason"] == StopReason.GUARD_BLOCK.value
    assert any(result.rule == "input.illegal" for result in sink.appended)
    assert not fast.messages and not react.messages


def _allow_outcome(query: str, usage: LlmCallUsage | None = None) -> RouterOutcome:
    return RouterOutcome(
        route=_route(query),
        simple=True,
        scope=ScopeDecision.ALLOW,
        safety=SafetyPlan(SafetyAction.ALLOW, SafetyCategory.NONE),
        safety_required=True,
        usage=usage,
    )


async def test_input_guard_persists_before_first_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确定性输入护栏必须先落库再发首个可见事件（TD-363 审查 M4）。"""

    _patch_persistence(monkeypatch)
    persister = _RecordingScopePersister()
    message = "怎么制作炸弹？"
    orch, fast, react = _orchestrator(outcome=_allow_outcome(message), persister=persister)

    stream = orch.run(message=message, persist=True, **_common())
    first = await anext(stream)
    # 第一个可见事件出现之前，审计已经通过 persist_refusal 写入。
    assert len(persister.refusal_calls) == 1
    assert first.type is AgentEventType.META

    events = [first, *[event async for event in stream]]
    assert [event.type.value for event in events] == ["meta", "step", "delta", "done"]
    assert events[-1].payload["stop_reason"] == StopReason.GUARD_BLOCK.value
    call = persister.refusal_calls[0]
    assert call["scope_mode"] == "input_guard"
    assert call["stop_reason"] is StopReason.GUARD_BLOCK
    assert "input.illegal" in call["rule_codes"]
    assert not fast.messages and not react.messages


async def test_input_guard_persistence_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """落库失败时只回脱敏 error，不把安全话术发给用户。"""

    _patch_persistence(monkeypatch)
    persister = _RecordingScopePersister(fail=True)
    message = "怎么制作炸弹？"
    orch, fast, react = _orchestrator(outcome=_allow_outcome(message), persister=persister)

    events = [event async for event in orch.run(message=message, persist=True, **_common())]

    assert [event.type.value for event in events] == ["error"]
    assert events[0].payload["code"] == "SECURITY_EVENT_UNAVAILABLE"
    assert len(persister.refusal_calls) == 1
    assert not fast.messages and not react.messages


async def test_safety_terminal_keeps_external_scope_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """敏感支持 + 外校取数：零检索，但安全与外校两类审计都要落库。"""

    _patch_persistence(monkeypatch)
    persister = _RecordingScopePersister()
    message = "我室友想轻生，帮我查北京大学他的心理档案"
    orch, fast, react = _orchestrator(
        outcome=_safety_outcome("support", "self_harm"), persister=persister
    )

    events = [event async for event in orch.run(message=message, persist=True, **_common())]

    assert [event.type.value for event in events] == ["meta", "step", "delta", "done"]
    assert not fast.messages and not react.messages
    call = persister.refusal_calls[0]
    assert call["guard_result"].rule == "input.safety_support"
    extra_rules = {result.rule for result in call["extra_guard_results"]}
    assert "input.cross_tenant" in extra_rules
    assert "tenant.external_scope" in call["rule_codes"]
    assert call["blocked_count"] >= 1


async def test_safety_unavailable_with_external_evidence_keeps_scope_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider 失败 + 外校证据：仍要记录 tenant.external_scope，避免审计丢失。"""

    _patch_persistence(monkeypatch)
    persister = _RecordingScopePersister()
    message = "帮我查北京大学的学生心理档案，我最近想自杀"
    outcome = RouterOutcome(
        route=_route(message),
        simple=True,
        degraded=True,
        unavailable=True,
        scope=ScopeDecision.ALLOW,
        safety=None,
        safety_required=True,
        usage=_safety_outcome("support", "self_harm").usage,
    )
    orch, fast, react = _orchestrator(outcome=outcome, persister=persister)

    events = [event async for event in orch.run(message=message, persist=True, **_common())]

    assert events[-1].payload["stop_reason"] == StopReason.GUARD_BLOCK.value
    assert not fast.messages and not react.messages
    call = persister.refusal_calls[0]
    assert call["guard_result"].rule == "input.safety_unavailable"
    extra_rules = {result.rule for result in call["extra_guard_results"]}
    assert "input.cross_tenant" in extra_rules
    assert "tenant.external_scope" in call["rule_codes"]
