"""ChatOrchestrator 统一入口（M4-AI01c + M4-AI06/07 会话持久化）。

事件流：orchestrator 发 `meta` + `step(route)`（首事件立出），路由结果传入对应
loop（loop 跳过自身 route 段）。loop 事件透传给消费方，orchestrator 同时收集
assistant 回答文本与 done 数据，**回合结束后持久化** user+assistant 消息。

**TD-334**：`permissions` 必传。**B1 修复**：消息持久化 + 上下文注入 + trace/usage
落库在本层接线（不在 loop 层——loop 保持无状态纯计算）。
"""

import asyncio
import logging
import unicodedata
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from app.core.metrics import record_guard_hit
from app.harness.contracts import (
    AgentEvent,
    AgentEventType,
    GuardResult,
    Intent,
    LlmCallUsage,
    RouteResult,
    StopReason,
    TraceRecord,
)
from app.harness.guards.input import detect_input
from app.harness.guards.scripts import (
    SAFETY_UNAVAILABLE_TEXT,
    SENSITIVE_HISTORY_PLACEHOLDER,
    safety_response_script,
    safety_script_for,
)
from app.harness.guards.tenant_scope import (
    TENANT_SCOPE_RESTRICTION_ACK,
    TenantScopeMode,
    TenantScopePlan,
    allows_public_comparison,
    classify_tenant_scope,
    inspect_tenant_scope_evidence,
    sanitize_history_text,
    semantic_scope_plan,
    tenant_scope_boundary_script,
    tenant_scope_guard_result,
    tenant_scope_quote_guard_result,
)
from app.harness.loop import FastPathLoop
from app.harness.persistence import (
    get_context_messages,
    get_or_create_conversation,
    save_message,
)
from app.harness.react_loop import ReactLoop
from app.harness.router import (
    LlmRouter,
    RouterOutcome,
    SafetyAction,
    SafetyPlan,
    ScopeDecision,
)

logger = logging.getLogger(__name__)

SCOPE_PERSISTENCE_ERROR_TEXT = "安全事件暂时无法记录，请稍后重试。"
ROUTER_UNAVAILABLE_TEXT = "AI 服务暂时不可用，请稍后重试。"
ROUTER_UNAVAILABLE_CODE = "LLM_ROUTER_UNAVAILABLE"
SAFETY_UNAVAILABLE_RULE = "input.safety_unavailable"


def _scope_error_event() -> AgentEvent:
    return AgentEvent(
        type=AgentEventType.ERROR,
        payload={
            "code": "SECURITY_EVENT_UNAVAILABLE",
            "message": SCOPE_PERSISTENCE_ERROR_TEXT,
            "retryable": True,
            "partial": False,
        },
    )


def _meta_event(*, turn_id: UUID, trace_id: str, conversation_id: UUID) -> AgentEvent:
    """构造统一的 meta 事件，避免各终止分支字段漂移。"""

    return AgentEvent(
        type=AgentEventType.META,
        payload={
            "turn_id": str(turn_id),
            "trace_id": trace_id,
            "conversation_id": str(conversation_id),
        },
    )


def _router_unavailable_event() -> AgentEvent:
    """范围路由故障的固定脱敏错误；不得拼接用户原文。"""

    return AgentEvent(
        type=AgentEventType.ERROR,
        payload={
            "code": ROUTER_UNAVAILABLE_CODE,
            "message": ROUTER_UNAVAILABLE_TEXT,
            "retryable": True,
            "partial": False,
        },
    )


def _query_traceable(query: str, safe_text: str) -> bool:
    """路由 query 规范化后必须连续来自已验证安全片段。"""

    compact_query = "".join(
        ch.lower() for ch in unicodedata.normalize("NFKC", query) if ch.isalnum()
    )
    compact_safe = "".join(
        ch.lower() for ch in unicodedata.normalize("NFKC", safe_text) if ch.isalnum()
    )
    return bool(compact_query) and compact_query in compact_safe


def _guard_trace_record(
    *,
    trace_id: str,
    tenant_id: UUID,
    user_id: UUID,
    conversation_id: UUID,
    turn_id: UUID,
    usage: LlmCallUsage | None,
    stop_reason: StopReason | None,
    error_code: str | None = None,
) -> TraceRecord:
    """构造只承载 router 调用的 guard trace，元数据不含任何提示词正文。"""

    return TraceRecord(
        trace_id=trace_id,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        intents=[],
        route_path="guard",
        provider=usage.provider if usage is not None else "",
        model=usage.model if usage is not None else "",
        prompt_name=usage.prompt_name if usage is not None else "",
        prompt_version=usage.prompt_version if usage is not None else "",
        prompt_hash=usage.prompt_hash if usage is not None else "",
        tokens_in=usage.tokens_in if usage is not None else 0,
        tokens_out=usage.tokens_out if usage is not None else 0,
        latency_ms=usage.latency_ms if usage is not None else 0,
        stop_reason=stop_reason,
        error_code=error_code,
        spans=[],
    )


class ChatOrchestrator:
    """统一对话入口。

    参数:
        router: LLM 路由器。
        fast_loop: 快路径管道（AI-01b）。
        react_loop: ReAct 循环（AI-01c）。
        guard_events_sink: 护栏事件落库端口（AI-09）。
    """

    def __init__(
        self,
        *,
        router: LlmRouter,
        fast_loop: FastPathLoop,
        react_loop: ReactLoop,
        guard_events_sink: Any | None = None,
        trace_sink: Any | None = None,
        scope_persister: Any | None = None,
    ) -> None:
        self._router = router
        self._fast_loop = fast_loop
        self._react_loop = react_loop
        self._guard_events_sink = guard_events_sink
        self._trace_sink = trace_sink
        self._scope_persister = scope_persister

    async def _flush_trace(self) -> None:
        """trace 缓冲落库（DbTraceSink；InMemory 无 flush 自动跳过）。"""

        flush = getattr(self._trace_sink, "flush", None)
        if flush is not None:
            try:
                await flush()
            except Exception:
                logger.exception("trace 落库失败（回合完成但 trace 未落）")

    async def _commit_scope_refusal(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        turn_id: UUID,
        conversation_id: UUID,
        trace_id: str,
        plan: TenantScopePlan,
        persist: bool,
        stop_reason: StopReason = StopReason.REFUSAL,
        scope_mode: str = "block",
        usage: LlmCallUsage | None = None,
    ) -> bool:
        """纯拒绝安全留痕；生产失败时返回 False，调用方只发脱敏 error。"""

        result = tenant_scope_guard_result(plan)
        if not persist:
            if self._guard_events_sink is not None:
                self._guard_events_sink.append(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    direction="input",
                    results=[result],
                )
            return True
        if self._scope_persister is None:
            logger.error("租户范围安全事件端口未装配")
            return False
        try:
            await self._scope_persister.persist_refusal(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                trace_id=trace_id,
                guard_result=result,
                rule_codes=plan.rule_codes,
                blocked_count=plan.blocked_count,
                stop_reason=stop_reason,
                scope_mode=scope_mode,
                call_usage=usage,
            )
        except Exception as exc:
            # 只记异常类型：SQLAlchemy 异常的 repr 可能带出审计密钥等敏感参数。
            logger.warning(
                "租户范围拒绝事件落库失败（fail-closed）：%s.%s",
                type(exc).__module__,
                type(exc).__name__,
            )
            return False
        self._record_scope_metrics(plan, severity=result.severity)
        return True

    async def _commit_scope_event(
        self,
        *,
        tenant_id: UUID,
        trace_id: str,
        plan: TenantScopePlan,
        safe_intents: tuple[str, ...],
        degraded: bool,
        scope_finalized: bool,
        persist: bool,
        usage: LlmCallUsage | None = None,
    ) -> bool:
        """混合请求在首个 SSE 前提交 guard event 与脱敏 scope span。"""

        result = tenant_scope_guard_result(plan)
        if not persist:
            if self._guard_events_sink is not None:
                self._guard_events_sink.append(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    direction="input",
                    results=[result],
                )
            return True
        if self._scope_persister is None:
            logger.error("租户范围安全事件端口未装配")
            return False
        try:
            await self._scope_persister.persist_event(
                tenant_id=tenant_id,
                trace_id=trace_id,
                guard_result=result,
                rule_codes=plan.rule_codes,
                blocked_count=plan.blocked_count,
                safe_intents=safe_intents,
                degraded=degraded,
                scope_finalized=scope_finalized,
                call_usage=usage,
            )
        except Exception as exc:
            logger.warning(
                "租户范围混合事件落库失败（fail-closed）：%s.%s",
                type(exc).__module__,
                type(exc).__name__,
            )
            return False
        self._record_scope_metrics(plan)
        return True

    async def _update_scope_event(
        self,
        *,
        tenant_id: UUID,
        trace_id: str,
        plan: TenantScopePlan,
        safe_intents: tuple[str, ...],
        degraded: bool,
        persist: bool,
    ) -> bool:
        """路由完成后补齐同一 scope span；基础事件已在 router 前提交。"""

        if not persist:
            return True
        if self._scope_persister is None:
            logger.error("租户范围安全事件端口未装配")
            return False
        try:
            await self._scope_persister.finalize_event(
                tenant_id=tenant_id,
                trace_id=trace_id,
                rule_codes=plan.rule_codes,
                blocked_count=plan.blocked_count,
                safe_intents=safe_intents,
                degraded=degraded,
            )
        except Exception as exc:
            logger.warning(
                "租户范围混合事件补充失败（fail-closed）：%s.%s",
                type(exc).__module__,
                type(exc).__name__,
            )
            return False
        return True

    async def _commit_scope_quote_event(
        self,
        *,
        tenant_id: UUID,
        trace_id: str,
        plan: TenantScopePlan,
        persist: bool,
    ) -> bool:
        """引述内跨租指令被净化但整体放行：先留痕再放行。

        落库失败时返回 False，调用方按 fail-closed 收口（与纯拒绝/混合两条路径
        一致）：简报要求“净化后放行必须留下 redact 审计事件并计指标”，
        审计不可见的放行等于没有护栏（十轮审查 m3）。
        """

        result = tenant_scope_quote_guard_result(plan.rule_codes)
        if not persist:
            if self._guard_events_sink is not None:
                self._guard_events_sink.append(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    direction="input",
                    results=[result],
                )
            return True
        if self._scope_persister is None:
            logger.error("租户范围安全事件端口未装配")
            return False
        try:
            await self._scope_persister.persist_event(
                tenant_id=tenant_id,
                trace_id=trace_id,
                guard_result=result,
                rule_codes=plan.rule_codes,
                blocked_count=plan.blocked_count,
                safe_intents=(),
                degraded=False,
                mode="quote_redacted",
            )
        except Exception as exc:
            # 只记异常类型：SQLAlchemy 异常的 repr 可能带出审计密钥等敏感参数。
            logger.warning(
                "租户范围引述净化事件落库失败（fail-closed）：%s.%s",
                type(exc).__module__,
                type(exc).__name__,
            )
            return False
        self._record_scope_metrics(plan, severity="medium")
        return True

    def _record_scope_metrics(self, plan: TenantScopePlan, *, severity: str = "high") -> None:
        for code in plan.rule_codes:
            record_guard_hit(code, severity, "input")

    @staticmethod
    def _safety_supplementary_scope_plan(
        scope_plan: TenantScopePlan, evidence: Any
    ) -> TenantScopePlan | None:
        """安全终止时若仍带外校取数证据，补一条 tenant.external_scope 审计计划。"""

        if (
            scope_plan.mode is TenantScopeMode.MIXED
            or evidence.external_access_candidate
            or evidence.bypass
        ):
            return TenantScopePlan(
                mode=(
                    TenantScopeMode.MIXED
                    if scope_plan.mode is TenantScopeMode.MIXED
                    else TenantScopeMode.BLOCK
                ),
                safe_text=None,
                rule_codes=("tenant.external_scope",),
                blocked_count=max(1, scope_plan.blocked_count),
            )
        return None

    async def _persist_route_usage(
        self,
        *,
        tenant_id: UUID,
        trace_id: str,
        usage: LlmCallUsage | None,
        persist: bool,
    ) -> None:
        """把 ALLOW 的 router 调用写成独立 span；可观测落库失败不阻断回答。"""

        if not persist or usage is None:
            return
        if self._scope_persister is None:
            logger.error("router usage 端口未装配")
            return
        persist_route_usage = getattr(self._scope_persister, "persist_route_usage", None)
        if persist_route_usage is None:
            logger.error("router usage 端口不支持 persist_route_usage")
            return
        try:
            await persist_route_usage(
                tenant_id=tenant_id,
                trace_id=trace_id,
                call_usage=usage,
            )
        except Exception as exc:
            logger.warning(
                "router usage 落库失败（不阻断回答）：%s.%s",
                type(exc).__module__,
                type(exc).__name__,
            )

    @staticmethod
    def _fallback_route_result(safe_text: str) -> RouteResult:
        """构造只引用已验证安全片段的单意图路由结果。"""

        return RouteResult(
            intents=[
                Intent(
                    name="kb_qa",
                    confidence=0.5,
                    args={"query": safe_text},
                )
            ],
            primary_capability="kb_qa",
        )

    @classmethod
    def _safe_route_fallback(
        cls,
        safe_text: str,
        *,
        safety_required: bool = False,
    ) -> RouterOutcome:
        """构造只引用已验证安全片段且不可进入数据面的降级结果。"""

        return RouterOutcome(
            route=cls._fallback_route_result(safe_text),
            simple=True,
            degraded=True,
            unavailable=True,
            safety_required=safety_required,
        )

    async def _route_scoped(
        self,
        safe_text: str,
        *,
        school_names: frozenset[str],
    ) -> RouterOutcome:
        """调用 scope-aware router；协议或调用失败退化为无 scope 的单意图。"""

        try:
            outcome = await self._router.route(safe_text)
        except Exception:
            logger.warning("路由调用失败（降级为单意图）")
            return self._safe_route_fallback(
                safe_text,
                safety_required=getattr(self._router, "requires_safety", False),
            )
        try:
            return self._scope_route(
                outcome,
                safe_text=safe_text,
                school_names=school_names,
            )
        except Exception:
            logger.warning("路由输出非法（fail-closed）")
            return self._safe_route_fallback(
                safe_text,
                safety_required=getattr(self._router, "requires_safety", False),
            )

    def _scope_route(
        self,
        outcome: RouterOutcome,
        *,
        safe_text: str,
        school_names: frozenset[str],
    ) -> RouterOutcome:
        """移除不可追溯或含外校取数证据的 query，仅保留已验证文本。"""

        if outcome.route is None:
            return outcome
        kept: list[Intent] = []
        degraded = outcome.degraded
        for intent in outcome.route.intents:
            raw_query = intent.args.get("query") if isinstance(intent.args, dict) else None
            query = str(raw_query) if raw_query else safe_text
            evidence = inspect_tenant_scope_evidence(query, school_names=school_names)
            candidate = query
            if (
                not _query_traceable(candidate, safe_text)
                or bool(detect_input(candidate))
                or (evidence.external_access_candidate and not allows_public_comparison(candidate))
            ):
                candidate = safe_text
                degraded = True
            kept.append(
                Intent(
                    name=intent.name,
                    confidence=intent.confidence,
                    args={"query": candidate},
                )
            )
        if not kept:
            kept = [
                Intent(
                    name="kb_qa",
                    confidence=0.5,
                    args={"query": safe_text},
                )
            ]
            degraded = True
        simple = len(kept) == 1 and kept[0].name == "kb_qa"
        return RouterOutcome(
            route=RouteResult(
                intents=kept,
                primary_capability=kept[0].name,
                needs_clarification=outcome.route.needs_clarification,
            ),
            simple=simple,
            degraded=degraded,
            scope=outcome.scope,
            safe_fragments=outcome.safe_fragments,
            blocked_count=outcome.blocked_count,
            unavailable=outcome.unavailable,
            usage=outcome.usage,
            safety=outcome.safety,
            safety_required=outcome.safety_required,
        )

    async def _save_completed_messages(
        self,
        *,
        tenant_id: UUID,
        conversation_id: UUID,
        trace_id: str,
        user_message: str,
        assistant_text: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        context_eligible: bool = True,
    ) -> None:
        try:
            status = "completed" if context_eligible else "partial"
            await save_message(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                role="user",
                content=user_message,
                trace_id=trace_id,
                status=status,
            )
            await save_message(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                role="assistant",
                content=assistant_text,
                trace_id=trace_id,
                status=status,
                token_usage={"tokens_in": tokens_in, "tokens_out": tokens_out},
            )
        except Exception:
            logger.exception("消息持久化失败（回答已完成但未落库）")

    async def _save_failed_messages(
        self,
        *,
        tenant_id: UUID,
        conversation_id: UUID,
        trace_id: str,
        user_message: str,
        context_eligible: bool = True,
    ) -> None:
        try:
            await save_message(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                role="user",
                content=user_message,
                trace_id=trace_id,
                status="completed" if context_eligible else "partial",
            )
            await save_message(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                role="assistant",
                content="",
                trace_id=trace_id,
                status="failed",
            )
        except Exception:
            logger.exception("错误回合消息持久化失败")

    async def _commit_input_guard(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        turn_id: UUID,
        conversation_id: UUID,
        trace_id: str,
        hits: list[GuardResult],
        persist: bool,
        usage: LlmCallUsage | None,
    ) -> bool:
        """确定性输入护栏在首个 SSE 前可靠落库（TD-363 审查 M4）。

        多条命中合并为同一事务的多行 guard_event，span.rule_codes 记录全部规则；
        与安全/范围拒绝走同一条 persist_refusal 端口，失败时只发脱敏 error。
        """

        if not hits:
            return True
        if not persist:
            if self._guard_events_sink is not None:
                self._guard_events_sink.append(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    direction="input",
                    results=list(hits),
                )
            for result in hits:
                record_guard_hit(result.rule, result.severity, "input")
            return True
        if self._scope_persister is None:
            logger.error("输入护栏安全事件端口未装配")
            return False
        try:
            await self._scope_persister.persist_refusal(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                trace_id=trace_id,
                guard_result=hits[0],
                extra_guard_results=tuple(hits[1:]),
                rule_codes=tuple(result.rule for result in hits),
                blocked_count=0,
                stop_reason=StopReason.GUARD_BLOCK,
                scope_mode="input_guard",
                call_usage=usage,
            )
        except Exception as exc:
            logger.warning(
                "输入护栏事件落库失败（fail-closed）：%s.%s",
                type(exc).__module__,
                type(exc).__name__,
            )
            return False
        for result in hits:
            record_guard_hit(result.rule, result.severity, "input")
        return True

    @staticmethod
    def _safety_guard_result(plan: SafetyPlan | None) -> GuardResult:
        """把安全计划投影为不含原文的审计事件。"""

        if plan is None:
            return GuardResult(
                action="block",
                rule=SAFETY_UNAVAILABLE_RULE,
                severity="high",
                reason="safety_plan_unavailable",
            )
        rule = (
            "input.safety_support" if plan.action is SafetyAction.SUPPORT else "input.safety_refuse"
        )
        return GuardResult(
            action="block",
            rule=rule,
            severity="high",
            reason=f"category={plan.category.value}",
        )

    async def _commit_safety_event(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        turn_id: UUID,
        conversation_id: UUID,
        trace_id: str,
        plan: SafetyPlan | None,
        persist: bool,
        usage: LlmCallUsage | None,
        supplementary_scope_plan: TenantScopePlan | None = None,
    ) -> tuple[bool, StopReason, GuardResult]:
        """首个可见事件前提交 guard/trace/span；提交失败则只发脱敏错误。

        ``supplementary_scope_plan``：安全终止与外校取数证据叠加时（如“我室友
        想轻生，帮我查他心理档案”），安全话术不能吞掉范围审计。此时同一事务里
        同时落安全 guard 和 tenant.external_scope 证据（TD-363 审查 M4）。
        """

        guard_result = self._safety_guard_result(plan)
        stop_reason = (
            StopReason.GUARD_BLOCK
            if plan is None or plan.action is SafetyAction.SUPPORT
            else StopReason.REFUSAL
        )
        scope_mode = "safety_unavailable" if plan is None else f"safety_{plan.action.value}"
        extra_results: tuple[GuardResult, ...] = ()
        extra_rule_codes: tuple[str, ...] = ()
        extra_blocked = 0
        if supplementary_scope_plan is not None:
            extra_results = (tenant_scope_guard_result(supplementary_scope_plan),)
            extra_rule_codes = supplementary_scope_plan.rule_codes
            extra_blocked = supplementary_scope_plan.blocked_count
        all_results = (guard_result, *extra_results)
        if not persist:
            if self._guard_events_sink is not None:
                self._guard_events_sink.append(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    direction="input",
                    results=list(all_results),
                )
            for result in all_results:
                record_guard_hit(result.rule, result.severity, "input")
            return True, stop_reason, guard_result
        if self._scope_persister is None:
            logger.error("安全计划事件端口未装配")
            return False, stop_reason, guard_result
        try:
            await self._scope_persister.persist_refusal(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                trace_id=trace_id,
                guard_result=guard_result,
                extra_guard_results=extra_results,
                rule_codes=(guard_result.rule, *extra_rule_codes),
                blocked_count=extra_blocked,
                stop_reason=stop_reason,
                scope_mode=scope_mode,
                call_usage=usage,
            )
        except Exception as exc:
            logger.warning(
                "安全计划事件落库失败（fail-closed）：%s.%s",
                type(exc).__module__,
                type(exc).__name__,
            )
            return False, stop_reason, guard_result
        for result in all_results:
            record_guard_hit(result.rule, result.severity, "input")
        return True, stop_reason, guard_result

    async def _safety_events(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        turn_id: UUID,
        conversation_id: UUID,
        trace_id: str,
        plan: SafetyPlan | None,
        persist: bool,
        usage: LlmCallUsage | None = None,
        supplementary_scope_plan: TenantScopePlan | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """安全终止路径：先审计，再向用户发送固定模板，不进入数据面。"""

        committed, stop_reason, _guard_result = await self._commit_safety_event(
            tenant_id=tenant_id,
            user_id=user_id,
            turn_id=turn_id,
            conversation_id=conversation_id,
            trace_id=trace_id,
            plan=plan,
            persist=persist,
            usage=usage,
            supplementary_scope_plan=supplementary_scope_plan,
        )
        if not committed:
            yield _scope_error_event()
            return
        script = (
            SAFETY_UNAVAILABLE_TEXT
            if plan is None
            else safety_response_script(plan.action, plan.category)
        )
        yield _meta_event(
            turn_id=turn_id,
            trace_id=trace_id,
            conversation_id=conversation_id,
        )
        yield AgentEvent(
            type=AgentEventType.STEP,
            payload={"stage": "route", "label": "正在判断安全处理方式"},
        )
        yield AgentEvent(type=AgentEventType.DELTA, payload={"text": script})
        yield AgentEvent(
            type=AgentEventType.DONE,
            payload={
                "turn_id": str(turn_id),
                "trace_id": trace_id,
                "conversation_id": str(conversation_id),
                "tokens_in": 0,
                "tokens_out": 0,
                "first_token_ms": 0,  # nosec B105——计时字段非密码
                "latency_ms": 0,
                "stop_reason": stop_reason.value,
            },
        )
        if persist:
            # 自伤/违法等敏感原文不得明文入库：历史只留固定占位，助手固定话术可保留。
            await self._save_completed_messages(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                trace_id=trace_id,
                user_message=SENSITIVE_HISTORY_PLACEHOLDER,
                assistant_text=script,
                context_eligible=False,
            )

    async def _refusal_events(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        turn_id: UUID,
        conversation_id: UUID,
        trace_id: str,
        message: str,
        plan: TenantScopePlan,
        persist: bool,
        stop_reason: StopReason = StopReason.REFUSAL,
        scope_mode: str = "block",
        usage: LlmCallUsage | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """安全留痕成功后才发首个可见事件。"""

        persisted = await self._commit_scope_refusal(
            tenant_id=tenant_id,
            user_id=user_id,
            turn_id=turn_id,
            conversation_id=conversation_id,
            trace_id=trace_id,
            plan=plan,
            persist=persist,
            stop_reason=stop_reason,
            scope_mode=scope_mode,
            usage=usage,
        )
        if not persisted:
            yield _scope_error_event()
            return
        script = tenant_scope_boundary_script(plan.rule_codes)
        yield AgentEvent(
            type=AgentEventType.META,
            payload={
                "turn_id": str(turn_id),
                "trace_id": trace_id,
                "conversation_id": str(conversation_id),
            },
        )
        yield AgentEvent(
            type=AgentEventType.STEP,
            payload={"stage": "route", "label": "正在理解问题"},
        )
        yield AgentEvent(type=AgentEventType.DELTA, payload={"text": script})
        yield AgentEvent(
            type=AgentEventType.DONE,
            payload={
                "turn_id": str(turn_id),
                "trace_id": trace_id,
                "conversation_id": str(conversation_id),
                "tokens_in": 0,
                "tokens_out": 0,
                "first_token_ms": 0,  # nosec B105——计时字段非密码
                "latency_ms": 0,
                "stop_reason": stop_reason.value,
            },
        )
        if persist:
            await self._save_completed_messages(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                trace_id=trace_id,
                user_message=message,
                assistant_text=script,
                context_eligible=False,
            )

    async def _restriction_events(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        turn_id: UUID,
        conversation_id: UUID,
        trace_id: str,
        message: str,
        persist: bool,
        usage: LlmCallUsage | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """纯禁止/拒绝外校取数：确认边界，不进入检索或工具。"""

        yield AgentEvent(
            type=AgentEventType.META,
            payload={
                "turn_id": str(turn_id),
                "trace_id": trace_id,
                "conversation_id": str(conversation_id),
            },
        )
        yield AgentEvent(
            type=AgentEventType.STEP,
            payload={"stage": "route", "label": "正在理解问题"},
        )
        yield AgentEvent(
            type=AgentEventType.DELTA,
            payload={"text": TENANT_SCOPE_RESTRICTION_ACK},
        )
        yield AgentEvent(
            type=AgentEventType.DONE,
            payload={
                "turn_id": str(turn_id),
                "trace_id": trace_id,
                "conversation_id": str(conversation_id),
                "tokens_in": 0,
                "tokens_out": 0,
                "first_token_ms": 0,  # nosec B105——计时字段非密码
                "latency_ms": 0,
                "stop_reason": StopReason.FINAL_ANSWER.value,
            },
        )
        if persist:
            await self._save_completed_messages(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                trace_id=trace_id,
                user_message=message,
                assistant_text=TENANT_SCOPE_RESTRICTION_ACK,
                context_eligible=False,
            )
            if self._trace_sink is not None:
                self._trace_sink.write_turn(
                    TraceRecord(
                        trace_id=trace_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        intents=[],
                        route_path="guard",
                        provider=usage.provider if usage is not None else "",
                        model=usage.model if usage is not None else "",
                        prompt_name=usage.prompt_name if usage is not None else "",
                        prompt_version=usage.prompt_version if usage is not None else "",
                        prompt_hash=usage.prompt_hash if usage is not None else "",
                        tokens_in=usage.tokens_in if usage is not None else 0,
                        tokens_out=usage.tokens_out if usage is not None else 0,
                        latency_ms=usage.latency_ms if usage is not None else 0,
                        stop_reason=StopReason.FINAL_ANSWER,
                        spans=[],
                    )
                )
                await self._flush_trace()

    async def run(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        turn_id: UUID,
        message: str,
        conversation_id: UUID | None = None,
        permissions: frozenset[str],
        school_names: frozenset[str] = frozenset(),
        persist: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        """执行一个对话回合；可疑范围证据先做语义路由，再进入检索/工具。"""

        trace_id = uuid4().hex
        conv_id = conversation_id if conversation_id is not None else uuid4()
        if persist:
            try:
                conv_id, _created = await get_or_create_conversation(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                )
            except Exception:
                logger.exception("会话创建/查询失败（消息不落库、trace 留痕）")
                conv_id = uuid4()

        evidence = inspect_tenant_scope_evidence(message, school_names=school_names)
        scope_plan = classify_tenant_scope(message, school_names=school_names)
        quote_redacted = scope_plan.mode is TenantScopeMode.ALLOW and bool(scope_plan.rule_codes)

        if quote_redacted:
            quote_persisted = await self._commit_scope_quote_event(
                tenant_id=tenant_id,
                trace_id=trace_id,
                plan=scope_plan,
                persist=persist,
            )
            if not quote_persisted:
                yield _scope_error_event()
                return

        if scope_plan.mode is TenantScopeMode.BLOCK:
            async for event in self._refusal_events(
                tenant_id=tenant_id,
                user_id=user_id,
                turn_id=turn_id,
                conversation_id=conv_id,
                trace_id=trace_id,
                message=message,
                plan=scope_plan,
                persist=persist,
                stop_reason=(
                    StopReason.GUARD_BLOCK
                    if "tenant.identity_claim_only" in scope_plan.rule_codes
                    else StopReason.REFUSAL
                ),
                scope_mode=(
                    "identity_claim"
                    if "tenant.identity_claim_only" in scope_plan.rule_codes
                    else "block"
                ),
            ):
                yield event
            return

        scoped_message = scope_plan.safe_text
        if not scoped_message:
            selected = TenantScopePlan(
                mode=TenantScopeMode.BLOCK,
                safe_text=None,
                rule_codes=("tenant.identity_claim_only",),
                blocked_count=max(1, scope_plan.blocked_count),
            )
            async for event in self._refusal_events(
                tenant_id=tenant_id,
                user_id=user_id,
                turn_id=turn_id,
                conversation_id=conv_id,
                trace_id=trace_id,
                message=message,
                plan=selected,
                persist=persist,
                stop_reason=StopReason.GUARD_BLOCK,
                scope_mode="identity_claim",
            ):
                yield event
            return

        outcome: RouterOutcome
        try:
            outcome = await self._route_scoped(
                scoped_message,
                school_names=school_names,
            )
        except (asyncio.CancelledError, GeneratorExit):
            # 首个可见事件之前路由被取消：无法证明消息不含外校数据请求，
            # 只落保守范围事件，随后保持取消语义，不进入数据面。
            cancellation_rules = tuple(
                sorted(set(scope_plan.rule_codes) | {"tenant.scope_router_cancelled"})
            )
            conservative_plan = TenantScopePlan(
                mode=TenantScopeMode.MIXED,
                safe_text=None,
                rule_codes=cancellation_rules,
                blocked_count=max(1, scope_plan.blocked_count),
            )
            await self._commit_scope_event(
                tenant_id=tenant_id,
                trace_id=trace_id,
                plan=conservative_plan,
                safe_intents=(),
                degraded=True,
                scope_finalized=False,
                persist=persist,
            )
            raise

        # 安全终止与外校取数证据可能同时存在；先算出补存范围审计计划，
        # 让安全话术与 tenant.external_scope 证据在同一事务落库（TD-363 审查 M4）。
        safety_supplementary_plan = self._safety_supplementary_scope_plan(scope_plan, evidence)

        if outcome.safety_unavailable:
            async for event in self._safety_events(
                tenant_id=tenant_id,
                user_id=user_id,
                turn_id=turn_id,
                conversation_id=conv_id,
                trace_id=trace_id,
                plan=None,
                persist=persist,
                usage=outcome.usage,
                supplementary_scope_plan=safety_supplementary_plan,
            ):
                yield event
            return

        if outcome.terminal_safety:
            async for event in self._safety_events(
                tenant_id=tenant_id,
                user_id=user_id,
                turn_id=turn_id,
                conversation_id=conv_id,
                trace_id=trace_id,
                plan=outcome.safety,
                persist=persist,
                usage=outcome.usage,
                supplementary_scope_plan=safety_supplementary_plan,
            ):
                yield event
            return
        if outcome.scope_unavailable:
            if evidence.external_access_candidate or evidence.bypass:
                failure_plan = TenantScopePlan(
                    mode=TenantScopeMode.BLOCK,
                    safe_text=None,
                    rule_codes=("tenant.external_scope",),
                    blocked_count=1,
                )
                async for event in self._refusal_events(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    turn_id=turn_id,
                    conversation_id=conv_id,
                    trace_id=trace_id,
                    message=message,
                    plan=failure_plan,
                    persist=persist,
                    usage=outcome.usage,
                ):
                    yield event
                return
            if persist and self._trace_sink is not None:
                self._trace_sink.write_turn(
                    _guard_trace_record(
                        trace_id=trace_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        conversation_id=conv_id,
                        turn_id=turn_id,
                        usage=outcome.usage,
                        stop_reason=None,
                        error_code=ROUTER_UNAVAILABLE_CODE,
                    )
                )
                await self._flush_trace()
            if persist:
                await self._save_failed_messages(
                    tenant_id=tenant_id,
                    conversation_id=conv_id,
                    trace_id=trace_id,
                    user_message=message,
                    context_eligible=False,
                )
            yield AgentEvent(
                type=AgentEventType.META,
                payload={
                    "turn_id": str(turn_id),
                    "trace_id": trace_id,
                    "conversation_id": str(conv_id),
                },
            )
            yield AgentEvent(
                type=AgentEventType.STEP,
                payload={"stage": "route", "label": "正在理解问题"},
            )
            yield _router_unavailable_event()
            return

        if outcome.scope is None:
            # 只有完整、合法的 intent-only 旧响应能保留 scope=None。当前模型
            # 的超时、非法 JSON 和 schema 冲突已由 unavailable 标记拦住。
            if evidence.external_access_candidate or evidence.bypass:
                failure_plan = TenantScopePlan(
                    mode=TenantScopeMode.BLOCK,
                    safe_text=None,
                    rule_codes=("tenant.external_scope",),
                    blocked_count=1,
                )
                async for event in self._refusal_events(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    turn_id=turn_id,
                    conversation_id=conv_id,
                    trace_id=trace_id,
                    message=message,
                    plan=failure_plan,
                    persist=persist,
                    usage=outcome.usage,
                ):
                    yield event
                return
        else:
            semantic_plan = semantic_scope_plan(
                outcome.scope.value,
                source=scoped_message,
                safe_fragments=outcome.safe_fragments,
                blocked_count=outcome.blocked_count,
                school_names=school_names,
            )
            if semantic_plan is None:
                failure_plan = TenantScopePlan(
                    mode=TenantScopeMode.BLOCK,
                    safe_text=None,
                    rule_codes=("tenant.external_scope",),
                    blocked_count=max(1, outcome.blocked_count),
                )
                async for event in self._refusal_events(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    turn_id=turn_id,
                    conversation_id=conv_id,
                    trace_id=trace_id,
                    message=message,
                    plan=failure_plan,
                    persist=persist,
                    usage=outcome.usage,
                ):
                    yield event
                return

            if outcome.scope is ScopeDecision.RESTRICTION:
                async for event in self._restriction_events(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    turn_id=turn_id,
                    conversation_id=conv_id,
                    trace_id=trace_id,
                    message=message,
                    persist=persist,
                    usage=outcome.usage,
                ):
                    yield event
                return

            if outcome.scope is ScopeDecision.IDENTITY_CLAIM:
                async for event in self._refusal_events(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    turn_id=turn_id,
                    conversation_id=conv_id,
                    trace_id=trace_id,
                    message=message,
                    plan=semantic_plan,
                    persist=persist,
                    stop_reason=StopReason.GUARD_BLOCK,
                    scope_mode="identity_claim",
                    usage=outcome.usage,
                ):
                    yield event
                return

            if outcome.scope is ScopeDecision.BLOCK:
                async for event in self._refusal_events(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    turn_id=turn_id,
                    conversation_id=conv_id,
                    trace_id=trace_id,
                    message=message,
                    plan=semantic_plan,
                    persist=persist,
                    usage=outcome.usage,
                ):
                    yield event
                return

            if (
                outcome.scope is ScopeDecision.ALLOW
                and evidence.external_access_candidate
                and not outcome.safe_fragments
                and not allows_public_comparison(scoped_message)
            ):
                failure_plan = TenantScopePlan(
                    mode=TenantScopeMode.BLOCK,
                    safe_text=None,
                    rule_codes=("tenant.external_scope",),
                    blocked_count=1,
                )
                async for event in self._refusal_events(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    turn_id=turn_id,
                    conversation_id=conv_id,
                    trace_id=trace_id,
                    message=message,
                    plan=failure_plan,
                    persist=persist,
                    usage=outcome.usage,
                ):
                    yield event
                return

            scope_plan = semantic_plan
            scoped_message = semantic_plan.safe_text or scoped_message
            outcome = self._scope_route(
                outcome,
                safe_text=scoped_message,
                school_names=school_names,
            )
            if outcome.route is None:
                failure_plan = TenantScopePlan(
                    mode=TenantScopeMode.BLOCK,
                    safe_text=None,
                    rule_codes=("tenant.external_scope",),
                    blocked_count=1,
                )
                async for event in self._refusal_events(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    turn_id=turn_id,
                    conversation_id=conv_id,
                    trace_id=trace_id,
                    message=message,
                    plan=failure_plan,
                    persist=persist,
                    usage=outcome.usage,
                ):
                    yield event
                return

            if scope_plan.mode is TenantScopeMode.MIXED:
                safe_intents = tuple(intent.name for intent in outcome.route.intents)
                persisted = await self._commit_scope_event(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    plan=scope_plan,
                    safe_intents=safe_intents,
                    degraded=outcome.degraded,
                    scope_finalized=True,
                    persist=persist,
                    usage=outcome.usage,
                )
                if not persisted:
                    yield _scope_error_event()
                    return

        input_hits = detect_input(scoped_message)
        if input_hits:
            # 先可靠落库 guard/trace/span，再发任何可见事件；落库失败只回脱敏 error，
            # 不能出现“审计没写成功但已经把话术发给用户”（TD-363 审查 M4）。
            committed = await self._commit_input_guard(
                tenant_id=tenant_id,
                user_id=user_id,
                turn_id=turn_id,
                conversation_id=conv_id,
                trace_id=trace_id,
                hits=input_hits,
                persist=persist,
                usage=outcome.usage,
            )
            if not committed:
                yield _scope_error_event()
                return
            script = safety_script_for({result.rule for result in input_hits})
            yield _meta_event(
                turn_id=turn_id,
                trace_id=trace_id,
                conversation_id=conv_id,
            )
            yield AgentEvent(
                type=AgentEventType.STEP,
                payload={"stage": "route", "label": "正在理解问题"},
            )
            yield AgentEvent(type=AgentEventType.DELTA, payload={"text": script})
            yield AgentEvent(
                type=AgentEventType.DONE,
                payload={
                    "turn_id": str(turn_id),
                    "trace_id": trace_id,
                    "conversation_id": str(conv_id),
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "first_token_ms": 0,  # nosec B105——计时字段非密码
                    "latency_ms": 0,
                    "stop_reason": StopReason.GUARD_BLOCK.value,
                },
            )
            if persist:
                # 注入/违法/自伤原文不落明文历史，历史只留固定占位。
                await self._save_completed_messages(
                    tenant_id=tenant_id,
                    conversation_id=conv_id,
                    trace_id=trace_id,
                    user_message=SENSITIVE_HISTORY_PLACEHOLDER,
                    assistant_text=script,
                    context_eligible=False,
                )
            return

        if scope_plan.mode is TenantScopeMode.ALLOW:
            await self._persist_route_usage(
                tenant_id=tenant_id,
                trace_id=trace_id,
                usage=outcome.usage,
                persist=persist,
            )

        yield _meta_event(
            turn_id=turn_id,
            trace_id=trace_id,
            conversation_id=conv_id,
        )
        yield AgentEvent(
            type=AgentEventType.STEP,
            payload={"stage": "route", "label": "正在理解问题"},
        )

        history: list[dict[str, str]] = []
        if persist:
            try:
                raw_history = await get_context_messages(
                    tenant_id=tenant_id,
                    conversation_id=conv_id,
                )
                for item in raw_history:
                    safe_content = sanitize_history_text(
                        item["content"],
                        school_names=school_names,
                    )
                    if safe_content:
                        history.append({"role": item["role"], "content": safe_content})
            except Exception:
                logger.exception("上下文查取失败（降级为无历史回合）")

        assistant_parts: list[str] = []
        done_data: dict[str, Any] | None = None
        error_data: dict[str, Any] | None = None
        if outcome.route is None:
            outcome = self._safe_route_fallback(scoped_message)
        route_result = outcome.route
        if route_result is None:
            route_result = self._fallback_route_result(scoped_message)
            outcome = self._safe_route_fallback(scoped_message)
        if outcome.simple:
            intent = route_result.intents[0]
            query = intent.args.get("query") if isinstance(intent.args, dict) else scoped_message
            query_text = str(query) if query else scoped_message
            event_stream = self._fast_loop.run(
                tenant_id=tenant_id,
                user_id=user_id,
                turn_id=turn_id,
                message=query_text,
                conversation_id=conv_id,
                permissions=permissions,
                skip_route_stage=True,
                trace_id=trace_id,
                history=history,
            )
        else:
            event_stream = self._react_loop.run(
                tenant_id=tenant_id,
                user_id=user_id,
                turn_id=turn_id,
                message=scoped_message,
                route=outcome,
                conversation_id=conv_id,
                permissions=permissions,
                skip_route_stage=True,
                trace_id=trace_id,
                history=history,
            )

        async for agent_event in event_stream:
            if agent_event.type == AgentEventType.DELTA:
                assistant_parts.append(agent_event.payload.get("text", ""))
            if agent_event.type == AgentEventType.DONE:
                done_data = agent_event.payload
                if scope_plan.mode is TenantScopeMode.MIXED:
                    boundary_text = tenant_scope_boundary_script(scope_plan.rule_codes)
                    boundary_event = AgentEvent(
                        type=AgentEventType.DELTA,
                        payload={"text": boundary_text},
                    )
                    assistant_parts.append(boundary_text)
                    yield boundary_event
            if agent_event.type == AgentEventType.ERROR:
                error_data = agent_event.payload
            yield agent_event

        if persist:
            await self._flush_trace()

        if persist and done_data is not None:
            await self._save_completed_messages(
                tenant_id=tenant_id,
                conversation_id=conv_id,
                trace_id=trace_id,
                user_message=message,
                assistant_text="".join(assistant_parts),
                tokens_in=int(done_data.get("tokens_in", 0)),
                tokens_out=int(done_data.get("tokens_out", 0)),
                context_eligible=(
                    not quote_redacted
                    and scope_plan.mode is TenantScopeMode.ALLOW
                    and not scope_plan.rule_codes
                ),
            )
        elif persist and error_data is not None:
            await self._save_failed_messages(
                tenant_id=tenant_id,
                conversation_id=conv_id,
                trace_id=trace_id,
                user_message=message,
                context_eligible=(
                    not quote_redacted
                    and scope_plan.mode is TenantScopeMode.ALLOW
                    and not scope_plan.rule_codes
                ),
            )
