"""Harness 契约校验测试。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.harness.contracts import (
    AgentEvent,
    AgentEventType,
    AgentRequest,
    GuardResult,
    Intent,
    RouteResult,
    StopReason,
    TenantContext,
    ToolSpec,
)


def tenant() -> TenantContext:
    """构造测试租户上下文。"""

    return TenantContext(tenant_id=uuid4(), user_id=uuid4())


def test_agent_request_requires_non_empty_message() -> None:
    """空消息违反输入边界。"""

    with pytest.raises(ValidationError):
        AgentRequest(
            tenant=tenant(),
            conversation_id=uuid4(),
            turn_id=uuid4(),
            message="",
        )


def test_route_result_requires_at_least_one_intent() -> None:
    """多意图路由结果至少一个意图。"""

    with pytest.raises(ValidationError):
        RouteResult(intents=[], primary_capability="kb_qa")


def test_intent_confidence_range() -> None:
    """意图置信度必须在 0..1。"""

    with pytest.raises(ValidationError):
        Intent(name="kb_qa", confidence=1.1)


def test_tool_spec_rejects_invalid_name_and_timeout() -> None:
    """工具名/超时非法时拒绝。"""

    with pytest.raises(ValidationError):
        ToolSpec(
            name="KB Search",
            version="1",
            description="x",
            side_effect=False,
            permission="kb.read",
            data_scope="self",
            timeout_ms=1000,
        )
    with pytest.raises(ValidationError):
        ToolSpec(
            name="kb.search",
            version="1",
            description="x",
            side_effect=False,
            permission="kb.read",
            data_scope="self",
            timeout_ms=0,
        )


def test_tool_spec_requires_explicit_side_effect_and_scope() -> None:
    """安全字段不允许缺省。"""

    with pytest.raises(ValidationError):
        ToolSpec(  # type: ignore[call-arg]
            name="kb.search",
            version="1",
            description="x",
            permission="kb.read",
            timeout_ms=1000,
        )


def test_stop_reason_and_event_values() -> None:
    """停止原因/事件类型是稳定字符串枚举。"""

    assert StopReason.MAX_STEPS.value == "MAX_STEPS"
    assert AgentEventType.DELTA.value == "delta"
    event = AgentEvent(type=AgentEventType.DELTA, payload={"text": "你好"})
    assert event.type is AgentEventType.DELTA


def test_guard_result_requires_action() -> None:
    """护栏结果必须有动作。"""

    result = GuardResult(action="block", rule="prompt_injection", reason="x")
    assert result.action == "block"
    with pytest.raises(ValidationError):
        GuardResult(action="unknown", rule="x")


def test_tenant_context_has_status_and_kb_versions() -> None:
    """租户上下文携带状态与固定 KB 版本。"""

    ctx = TenantContext(
        tenant_id=uuid4(),
        user_id=uuid4(),
        tenant_status="suspended",
        kb_version_ids=("v1",),
    )
    assert ctx.tenant_status == "suspended"
    assert ctx.kb_version_ids == ("v1",)
