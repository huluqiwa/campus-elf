"""Harness 核心契约。

这些模型是 Harness 与 LangGraph/工具/观测之间的稳定边界；不包含业务状态机。
安全相关模型使用 ``extra="forbid"``，避免拼写错误被静默忽略。
"""

from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class StopReason(StrEnum):
    """回合停止原因。"""

    FINAL_ANSWER = "FINAL_ANSWER"
    REFUSAL = "REFUSAL"
    CAPABILITY_FALLBACK = "CAPABILITY_FALLBACK"
    GUARD_BLOCK = "GUARD_BLOCK"
    TIMEOUT = "TIMEOUT"
    USER_CANCELLED = "USER_CANCELLED"
    MAX_STEPS = "MAX_STEPS"
    TOOL_BUDGET_EXCEEDED = "TOOL_BUDGET_EXCEEDED"
    REPEATED_CALL = "REPEATED_CALL"


class AgentEventType(StrEnum):
    """SSE 事件类型。"""

    META = "meta"
    STEP = "step"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    REASONING_SUMMARY = "reasoning_summary"
    DELTA = "delta"
    CITATION = "citation"
    DONE = "done"
    ERROR = "error"


class TenantContext(BaseModel):
    """请求级租户上下文。

    注意：这是服务端内部模型，必须由认证层从 JWT/会话注入，
    不能直接作为外部请求体接收，否则客户端可伪造 tenant_id/permissions。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    user_id: UUID
    tenant_status: Literal["active", "suspended", "deleted"] = "active"
    roles: frozenset[str] = Field(default_factory=frozenset)
    permissions: frozenset[str] = Field(default_factory=frozenset)
    data_scopes: tuple[str, ...] = ()
    kb_version_ids: tuple[str, ...] = ()
    feature_flags: dict[str, bool] = Field(default_factory=dict)


class AgentRequest(BaseModel):
    """一次对话回合的输入。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant: TenantContext
    conversation_id: UUID
    turn_id: UUID
    message: str = Field(min_length=1, max_length=8000)
    trace_id: str | None = Field(default=None, max_length=128)
    user_prefs: dict[str, Any] = Field(default_factory=dict)


class Intent(BaseModel):
    """路由识别出的单个意图。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    args: dict[str, Any] = Field(default_factory=dict)


class RouteResult(BaseModel):
    """多意图路由结果。"""

    model_config = ConfigDict(extra="forbid")

    intents: list[Intent] = Field(min_length=1)
    primary_capability: str = Field(min_length=1)
    needs_clarification: bool = False


class AgentEvent(BaseModel):
    """SSE 事件载荷。"""

    model_config = ConfigDict(extra="forbid")

    type: AgentEventType
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolSpec(BaseModel):
    """工具契约。

    ``side_effect`` 与 ``data_scope`` 必须显式声明，避免新写工具漏标后被当只读。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=64)
    version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    side_effect: bool
    permission: str = Field(min_length=1)
    data_scope: Literal["school", "class", "course", "self", "none"]
    timeout_ms: int = Field(gt=0, le=600000)
    idempotent: bool = True
    audit_level: Literal["none", "basic", "full"] = "basic"
    availability_flag: str | None = None


class ToolCall(BaseModel):
    """工具调用请求。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    call_id: UUID = Field(default_factory=uuid4)
    trace_id: str | None = Field(default=None, max_length=128)


class ToolResult(BaseModel):
    """工具调用结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    data: Any = None
    error_code: str | None = None
    duration_ms: int = Field(default=0, ge=0)
    redacted_summary: str = ""


class GuardResult(BaseModel):
    """输入/输出护栏结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["allow", "block", "redact", "fallback"]
    rule: str = Field(min_length=1)
    severity: Literal["low", "medium", "high"] = "low"
    reason: str = ""
    replacement: str | None = None


class TraceSpan(BaseModel):
    """单次 trace span。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str = Field(min_length=1)
    tenant_id: UUID | None = None
    span_type: str = Field(min_length=1)
    name: str = Field(min_length=1)
    start_ms: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    attributes: dict[str, Any] = Field(default_factory=dict)
    status: Literal["ok", "error"] = "ok"


class TraceRecord(BaseModel):
    """一次对话回合的本地 trace。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str = Field(min_length=1)
    tenant_id: UUID
    user_id: UUID
    conversation_id: UUID
    turn_id: UUID
    intents: list[str] = Field(default_factory=list)
    route_path: Literal["fast", "react"] = "fast"
    model: str = ""
    prompt_name: str = ""
    prompt_version: str = ""
    prompt_hash: str = ""
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    first_token_ms: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    stop_reason: StopReason | None = None
    error_code: str | None = None
    spans: list[TraceSpan] = Field(default_factory=list)
