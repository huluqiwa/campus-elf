"""Harness 契约与基础设施。

M0-C 只提供类型化契约、注册表、工具执行、trace、审计哈希链和 Prompt 注册表；
不包含 LangGraph 图、DB 持久化或真实工具实现。
"""

from app.harness.audit import AuditEvent, AuditSink, InMemoryAuditSink, redact_mapping
from app.harness.contracts import (
    AgentEvent,
    AgentEventType,
    AgentRequest,
    GuardResult,
    Intent,
    RouteResult,
    StopReason,
    TenantContext,
    ToolCall,
    ToolResult,
    ToolSpec,
    TraceRecord,
    TraceSpan,
)
from app.harness.prompts import PromptRegistry, PromptSpec
from app.harness.registry import (
    CapabilityRegistry,
    CapabilitySpec,
    ToolRegistry,
)
from app.harness.tools import ToolExecutor
from app.harness.trace import InMemoryTraceSink, TraceRecorder, TraceSink

__all__ = [
    "AgentEvent",
    "AgentEventType",
    "AgentRequest",
    "AuditEvent",
    "AuditSink",
    "CapabilityRegistry",
    "CapabilitySpec",
    "GuardResult",
    "InMemoryAuditSink",
    "InMemoryTraceSink",
    "Intent",
    "PromptRegistry",
    "PromptSpec",
    "RouteResult",
    "StopReason",
    "TenantContext",
    "ToolCall",
    "ToolExecutor",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "TraceRecord",
    "TraceRecorder",
    "TraceSink",
    "TraceSpan",
    "redact_mapping",
]
