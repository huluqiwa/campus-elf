"""ToolExecutor 策略/超时/schema/审计测试。"""

import asyncio
from uuid import uuid4

import pytest

from app.harness.audit import InMemoryAuditSink
from app.harness.contracts import TenantContext, ToolCall, ToolResult, ToolSpec
from app.harness.registry import ToolRegistry
from app.harness.tools import ToolExecutor
from app.harness.trace import InMemoryTraceSink, TraceRecorder

SECRET = "test-secret-key-123456"


def tenant(
    permissions: frozenset[str] = frozenset({"kb.read"}),
    data_scopes: tuple[str, ...] = ("school",),
    **kwargs: object,
) -> TenantContext:
    """构造测试租户上下文。"""

    return TenantContext(
        tenant_id=uuid4(),
        user_id=uuid4(),
        permissions=permissions,
        data_scopes=data_scopes,
        **kwargs,
    )


def spec(name: str = "kb.search", **kwargs: object) -> ToolSpec:
    """构造测试工具定义。"""

    fields: dict[str, object] = {
        "version": "1",
        "description": "test tool",
        "side_effect": False,
        "permission": "kb.read",
        "data_scope": "school",
        "timeout_ms": 1000,
    }
    fields.update(kwargs)
    return ToolSpec(name=name, **fields)


def make_executor(registry: ToolRegistry) -> ToolExecutor:
    """构造带显式 trace/audit 的执行器。"""

    return ToolExecutor(
        registry,
        trace=TraceRecorder(InMemoryTraceSink()),
        audit=InMemoryAuditSink(SECRET),
    )


async def ok_handler(call: ToolCall, ctx: TenantContext) -> ToolResult:
    """成功处理器。"""

    return ToolResult(ok=True, data=call.args)


async def slow_handler(call: ToolCall, ctx: TenantContext) -> ToolResult:
    """慢处理器，用于超时测试。"""

    await asyncio.sleep(0.2)
    return ToolResult(ok=True)


async def error_handler(call: ToolCall, ctx: TenantContext) -> ToolResult:
    """异常处理器。"""

    raise RuntimeError("boom")


async def non_toolresult_handler(call: ToolCall, ctx: TenantContext) -> ToolResult:
    """返回非 ToolResult，用于契约违反测试。"""

    return {"ok": True}  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_tool_executor_success_records_trace_and_audit() -> None:
    """成功调用写 trace span 与 audit 事件。"""

    registry = ToolRegistry()
    registry.register(spec(), ok_handler)
    trace_sink = InMemoryTraceSink()
    audit_sink = InMemoryAuditSink(SECRET)
    executor = ToolExecutor(
        registry,
        trace=TraceRecorder(trace_sink),
        audit=audit_sink,
    )
    call = ToolCall(tool="kb.search", args={"q": "奖学金"}, trace_id="trace-1")

    result = await executor.execute(call, tenant())

    assert result.ok is True
    assert result.data == {"q": "奖学金"}
    assert len(trace_sink.spans) == 1
    assert trace_sink.spans[0].status == "ok"
    assert trace_sink.spans[0].tenant_id is not None
    assert len(audit_sink.events) == 1
    assert audit_sink.events[0].action == "tool.execute"
    assert audit_sink.events[0].trace_id == "trace-1"
    assert audit_sink.verify() is True


@pytest.mark.asyncio
async def test_tool_executor_permission_denied() -> None:
    """无权限返回 PERMISSION_DENIED。"""

    registry = ToolRegistry()
    registry.register(spec(), ok_handler)
    result = await make_executor(registry).execute(
        ToolCall(tool="kb.search", args={}), tenant(permissions=frozenset())
    )
    assert result.ok is False
    assert result.error_code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_tool_executor_blocks_write_tools() -> None:
    """MVP 不允许 side_effect 工具执行。"""

    registry = ToolRegistry()
    registry.register(spec(side_effect=True), ok_handler, allow_write=True)
    result = await make_executor(registry).execute(
        ToolCall(tool="kb.search", args={}), tenant()
    )
    assert result.ok is False
    assert result.error_code == "WRITE_TOOL_DISABLED"


@pytest.mark.asyncio
async def test_tool_executor_unavailable_flag() -> None:
    """功能开关关闭时拒绝。"""

    registry = ToolRegistry()
    registry.register(spec(availability_flag="ENABLE_KB"), ok_handler)
    result = await make_executor(registry).execute(
        ToolCall(tool="kb.search", args={}), tenant()
    )
    assert result.ok is False
    assert result.error_code == "TOOL_UNAVAILABLE"


@pytest.mark.asyncio
async def test_tool_executor_data_scope_denied() -> None:
    """数据范围不足时拒绝。"""

    registry = ToolRegistry()
    registry.register(spec(data_scope="school"), ok_handler)
    result = await make_executor(registry).execute(
        ToolCall(tool="kb.search", args={}), tenant(data_scopes=("class",))
    )
    assert result.ok is False
    assert result.error_code == "DATA_SCOPE_DENIED"


async def test_tool_executor_broader_scope_allows_narrower() -> None:
    """全校范围可执行班级范围工具。"""

    registry = ToolRegistry()
    registry.register(spec(data_scope="class"), ok_handler)
    result = await make_executor(registry).execute(
        ToolCall(tool="kb.search", args={}), tenant(data_scopes=("school",))
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_tool_executor_tenant_inactive() -> None:
    """租户非 active 时拒绝。"""

    registry = ToolRegistry()
    registry.register(spec(), ok_handler)
    result = await make_executor(registry).execute(
        ToolCall(tool="kb.search", args={}), tenant(tenant_status="suspended")
    )
    assert result.ok is False
    assert result.error_code == "TENANT_INACTIVE"


@pytest.mark.asyncio
async def test_tool_executor_invalid_args() -> None:
    """入参不满足 input_schema 时拒绝。"""

    registry = ToolRegistry()
    schema = {
        "type": "object",
        "required": ["q"],
        "properties": {"q": {"type": "string"}},
    }
    registry.register(spec(input_schema=schema), ok_handler)
    result = await make_executor(registry).execute(
        ToolCall(tool="kb.search", args={}), tenant()
    )
    assert result.ok is False
    assert result.error_code == "INVALID_ARGS"


@pytest.mark.asyncio
async def test_tool_executor_invalid_result() -> None:
    """返回值不满足 output_schema 时拒绝。"""

    registry = ToolRegistry()
    schema = {
        "type": "object",
        "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
    }
    registry.register(spec(output_schema=schema), ok_handler)
    result = await make_executor(registry).execute(
        ToolCall(tool="kb.search", args={"q": "x"}), tenant()
    )
    assert result.ok is False
    assert result.error_code == "INVALID_RESULT"


@pytest.mark.asyncio
async def test_tool_executor_timeout() -> None:
    """超时返回 TOOL_TIMEOUT。"""

    registry = ToolRegistry()
    registry.register(spec(timeout_ms=10), slow_handler)
    result = await make_executor(registry).execute(
        ToolCall(tool="kb.search", args={}), tenant()
    )
    assert result.ok is False
    assert result.error_code == "TOOL_TIMEOUT"


@pytest.mark.asyncio
async def test_tool_executor_handler_error() -> None:
    """处理器异常返回 TOOL_ERROR，不泄露异常文本。"""

    registry = ToolRegistry()
    registry.register(spec(), error_handler)
    result = await make_executor(registry).execute(
        ToolCall(tool="kb.search", args={}), tenant()
    )
    assert result.ok is False
    assert result.error_code == "TOOL_ERROR"


async def test_tool_executor_non_toolresult_is_tool_error() -> None:
    """处理器违反返回类型时降级为 TOOL_ERROR，且仍写审计。"""

    registry = ToolRegistry()
    registry.register(spec(), non_toolresult_handler)
    audit_sink = InMemoryAuditSink(SECRET)
    executor = ToolExecutor(
        registry,
        trace=TraceRecorder(InMemoryTraceSink()),
        audit=audit_sink,
    )
    result = await executor.execute(ToolCall(tool="kb.search", args={}), tenant())
    assert result.ok is False
    assert result.error_code == "TOOL_ERROR"
    assert audit_sink.count == 1


async def test_tool_executor_invalid_schema() -> None:
    """schema 运行期解析失败时返回 INVALID_SCHEMA。"""

    registry = ToolRegistry()
    # $ref 指向不存在的本地定义：注册期 check_schema 通过，运行期解析失败。
    schema = {"$ref": "#/definitions/missing"}
    registry.register(spec(input_schema=schema), ok_handler)
    result = await make_executor(registry).execute(
        ToolCall(tool="kb.search", args={"q": "x"}), tenant()
    )
    assert result.ok is False
    assert result.error_code == "INVALID_SCHEMA"


def test_tool_executor_requires_trace_and_audit() -> None:
    """trace/audit 是必需依赖。"""

    with pytest.raises(TypeError):
        ToolExecutor(ToolRegistry())  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_tool_executor_cancelled_records_audit() -> None:
    """取消调用写 audit/trace 后重新抛出 CancelledError。"""

    registry = ToolRegistry()
    registry.register(spec(timeout_ms=1000), slow_handler)
    trace_sink = InMemoryTraceSink()
    audit_sink = InMemoryAuditSink(SECRET)
    executor = ToolExecutor(
        registry,
        trace=TraceRecorder(trace_sink),
        audit=audit_sink,
    )
    task = asyncio.create_task(
        executor.execute(ToolCall(tool="kb.search", args={}), tenant())
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert audit_sink.count == 1
    assert audit_sink.events[0].metadata["error_code"] == "CANCELLED"
    assert trace_sink.spans[0].status == "error"


async def test_tool_executor_missing_tool() -> None:
    """未注册工具返回 TOOL_NOT_FOUND。"""

    executor = ToolExecutor(
        ToolRegistry(),
        trace=TraceRecorder(InMemoryTraceSink()),
        audit=InMemoryAuditSink(SECRET),
    )
    result = await executor.execute(ToolCall(tool="kb.missing", args={}), tenant())
    assert result.ok is False
    assert result.error_code == "TOOL_NOT_FOUND"
