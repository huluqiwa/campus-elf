"""工具执行器。

MVP 只允许只读工具；执行链：
存在性 → 租户状态 → 权限 → 只读策略 → availability → data_scope
→ input schema → 超时执行 → output schema → trace/audit。
"""

import asyncio
import time

import jsonschema

from app.harness.audit import AuditEvent, AuditSink
from app.harness.contracts import TenantContext, ToolCall, ToolResult, TraceSpan
from app.harness.registry import ToolRegistry
from app.harness.trace import TraceRecorder

_SCOPE_RANK: dict[str, int] = {
    "none": 0,
    "self": 1,
    "course": 2,
    "class": 3,
    "school": 4,
}


def _scope_allows(required: str, ctx: TenantContext) -> bool:
    """判断用户数据范围是否覆盖工具要求；范围可向下兼容。"""

    if required == "none":
        return True
    required_rank = _SCOPE_RANK[required]
    return any(_SCOPE_RANK.get(scope, -1) >= required_rank for scope in ctx.data_scopes)


class ToolExecutor:
    """统一执行权限、租户、范围、schema、超时、trace 与审计。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        trace: TraceRecorder,
        audit: AuditSink,
    ) -> None:
        """绑定注册表与必需的观测/审计端口。"""

        self._registry = registry
        self._trace = trace
        self._audit = audit

    async def execute(self, call: ToolCall, ctx: TenantContext) -> ToolResult:
        """执行一次工具调用。"""

        start = time.monotonic()
        try:
            spec = self._registry.get_spec(call.tool)
        except KeyError:
            return self._finish(
                call, ctx, ToolResult(ok=False, error_code="TOOL_NOT_FOUND"), start
            )

        if ctx.tenant_status != "active":
            return self._finish(
                call, ctx, ToolResult(ok=False, error_code="TENANT_INACTIVE"), start
            )
        if spec.permission not in ctx.permissions:
            return self._finish(
                call, ctx, ToolResult(ok=False, error_code="PERMISSION_DENIED"), start
            )
        if spec.side_effect:
            return self._finish(
                call, ctx, ToolResult(ok=False, error_code="WRITE_TOOL_DISABLED"), start
            )
        if spec.availability_flag is not None and not ctx.feature_flags.get(
            spec.availability_flag, False
        ):
            return self._finish(
                call, ctx, ToolResult(ok=False, error_code="TOOL_UNAVAILABLE"), start
            )
        if not _scope_allows(spec.data_scope, ctx):
            return self._finish(
                call, ctx, ToolResult(ok=False, error_code="DATA_SCOPE_DENIED"), start
            )

        try:
            jsonschema.validate(instance=call.args, schema=spec.input_schema)
        except jsonschema.ValidationError:
            return self._finish(
                call, ctx, ToolResult(ok=False, error_code="INVALID_ARGS"), start
            )
        except jsonschema.SchemaError:
            return self._finish(
                call, ctx, ToolResult(ok=False, error_code="INVALID_SCHEMA"), start
            )

        handler = self._registry.get_handler(call.tool)
        try:
            result = await asyncio.wait_for(
                handler(call, ctx),
                timeout=spec.timeout_ms / 1000,
            )
        except asyncio.CancelledError:
            self._finish(call, ctx, ToolResult(ok=False, error_code="CANCELLED"), start)
            raise
        except TimeoutError:
            result = ToolResult(ok=False, error_code="TOOL_TIMEOUT")
        except Exception:
            # 不把异常文本写入结果，避免泄露内部地址/凭证。
            result = ToolResult(ok=False, error_code="TOOL_ERROR")

        if result.ok:
            try:
                jsonschema.validate(instance=result.data, schema=spec.output_schema)
            except jsonschema.ValidationError:
                result = ToolResult(ok=False, error_code="INVALID_RESULT")
            except jsonschema.SchemaError:
                result = ToolResult(ok=False, error_code="INVALID_SCHEMA")

        return self._finish(call, ctx, result, start)

    def _finish(
        self,
        call: ToolCall,
        ctx: TenantContext,
        result: ToolResult,
        start: float,
    ) -> ToolResult:
        """补齐耗时并写入 trace/audit。"""

        duration_ms = int((time.monotonic() - start) * 1000)
        result = result.model_copy(update={"duration_ms": duration_ms})
        trace_id = call.trace_id or f"tool-{call.call_id}"
        self._trace.record_span(
            TraceSpan(
                trace_id=trace_id,
                tenant_id=ctx.tenant_id,
                span_type="tool",
                name=call.tool,
                start_ms=int(time.time() * 1000),
                duration_ms=duration_ms,
                attributes={
                    "call_id": str(call.call_id),
                    "ok": result.ok,
                    "error_code": result.error_code,
                },
                status="ok" if result.ok else "error",
            )
        )
        self._audit.append(
            AuditEvent(
                tenant_id=ctx.tenant_id,
                actor_id=ctx.user_id,
                action="tool.execute",
                resource_type="tool",
                resource_id=call.tool,
                result="success" if result.ok else "error",
                metadata={
                    "call_id": str(call.call_id),
                    "ok": result.ok,
                    "error_code": result.error_code,
                },
            )
        )
        return result
