"""ReAct 循环（M4-AI01c 步 3）：THINK→ACT→OBSERVE 有界 → 合并 GENERATE。

决策卡 3-A/4-A/5-A、D2-D5：

- **THINK**：非流式 `complete()` + `tool_specs`（function calling，卡 3-A）——
  返回 `tool_calls` 即动作决策；返回纯文本视为「思考结束」（进 GENERATE）。
- **ACT**：`ToolExecutor` 全链执行；OBSERVE 以 `[观察]` 文本消息回喂（不扩展
  LlmMessage 契约——provider 无关）。
- **预算**：`TurnBudget` 全局计数（决策/工具/检索/token）；任一耗尽 → 立即带
  已有证据 GENERATE（降级不报错，卡 5-A）；budget 状态落 span（D5）。
- **navigate 结果**作为入口证据并入证据列表（citations 全局编号；GENERATE 可
  引用 route/label）。
- **kb.read_chunk 白名单**不变：未检索先读 → `CHUNK_NOT_IN_EVIDENCE` 观察反馈
  （循环继续，防 ACL 旁路）。
- **多意图合并作答**（卡 4-A）：react_v1 要求逐意图小节、某意图零证据 → 该小节
  兜底话术（逐项兜底，不整题拒答）。

超时（对齐 AI-01b 口径）：`turn_timeout_s` 包**循环全程 + GENERATE 首事件**；
delta 后按客户端节奏。trace：`route_path="react"`、`intents=路由意图名列表`、
budget span。
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from app.core.metrics import record_guard_hit
from app.harness.audit import AuditSink
from app.harness.budget import TurnBudget
from app.harness.chat_tools import (
    CAMPUS_NAVIGATE,
    KB_SEARCH,
    TurnToolState,
    build_turn_tools,
    openai_tool_specs,
)
from app.harness.contracts import (
    AgentEvent,
    AgentEventType,
    StopReason,
    TenantContext,
    ToolCall,
    TraceRecord,
    TraceSpan,
)
from app.harness.contracts import GuardResult as GuardResultContract
from app.harness.guards.stream_guard import StreamSentinel
from app.harness.llm import LlmError, LlmMessage, LlmPort, LlmRequest, LlmStreamEvent
from app.harness.loop import format_evidence
from app.harness.prompts import PromptRegistry
from app.harness.router import RouterOutcome
from app.harness.tools import ToolExecutor
from app.harness.trace import TraceRecorder, TraceSink
from app.kb.chat_retrieval import ChatEvidence, ChatRetriever

logger = logging.getLogger(__name__)

REACT_THINK_PROMPT = "react_think"
REACT_ANSWER_PROMPT = "react"
PROMPT_VERSION = "v1"

# 兜底话术（与快路径同文案——用户视角一致的兜底体验）
FALLBACK_TEXT = (
    "抱歉，我暂时没有在学校知识库中找到与这个问题相关的内容。"
    "你可以换个说法再试一次，或联系管理员确认相关资料是否已发布。"
)
LLM_UNCONFIGURED_TEXT = "AI 回答能力尚未配置，请联系管理员。"


def _intents_block(route: RouterOutcome) -> str:
    """路由意图 → 模板块（逐意图子问题清单）。"""

    lines = []
    for index, intent in enumerate(route.route.intents, start=1):
        query = intent.args.get("query") if isinstance(intent.args, dict) else None
        lines.append(f"{index}. [{intent.name}] {query or ''}")
    return "\n".join(lines)


class ReactLoop:
    """ReAct 编排器（use-site 构造；端口注入对齐既有纪律）。

    参数:
        llm_port: LLM 端口；None = 未配置（THINK 前 error 兜底）。
        retriever: chat 检索器（kb.search 工具面 + 证据文本回查）。
        prompt_registry: 生产 Prompt 注册表（react_think/react 模板）。
        trace_sink/audit_sink: 观测与审计端口（AI-01b 同款）。
        turn_timeout_s: 循环全程 + GENERATE 首事件硬超时（秒）。
        retrieve_timeout_s: 证据回查 wait_for 上限（秒）。
        max_*: 预算上限（design §6.2 数值，Settings 化传入）。
    """

    def __init__(
        self,
        *,
        llm_port: LlmPort | None,
        retriever: ChatRetriever,
        prompt_registry: PromptRegistry,
        trace_sink: TraceSink,
        audit_sink: AuditSink,
        guard_events_sink: Any | None = None,
        turn_timeout_s: float = 30.0,
        retrieve_timeout_s: float = 5.0,
        max_decisions: int = 5,
        max_tool_calls: int = 3,
        max_retrievals: int = 2,
        token_budget: int = 12000,
    ) -> None:
        self._llm_port = llm_port
        self._retriever = retriever
        self._registry = prompt_registry
        self._trace_sink = trace_sink
        self._audit_sink = audit_sink
        self._guard_events_sink = guard_events_sink
        self._turn_timeout_s = turn_timeout_s
        self._retrieve_timeout_s = retrieve_timeout_s
        self._max_decisions = max_decisions
        self._max_tool_calls = max_tool_calls
        self._max_retrievals = max_retrievals
        self._token_budget = token_budget

    async def run(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        turn_id: UUID,
        message: str,
        route: RouterOutcome,
        conversation_id: UUID | None = None,
        permissions: frozenset[str] | None = None,
        skip_route_stage: bool = False,
        trace_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """执行一个 ReAct 回合；产出 SSE 事件（类型化 AgentEvent）。

        参数:
            route: 路由结果（orchestrator 传入；standalone 调用直接给 RouterOutcome）。
            permissions: principal 实际权限集（TD-334——ctx 从实际权限派生）；
                None 仅限测试/兼容，生产路径必传。
            skip_route_stage: True = orchestrator 已发 meta/step(route)（本 loop 跳过）。
        """

        resolved_trace = trace_id if trace_id is not None else uuid4().hex
        conversation = conversation_id if conversation_id is not None else uuid4()
        started = time.perf_counter()
        recorder = TraceRecorder(self._trace_sink)
        state = TurnToolState()
        registry = build_turn_tools(retriever=self._retriever, state=state)
        executor = ToolExecutor(registry, trace=recorder, audit=self._audit_sink)
        ctx = TenantContext(
            tenant_id=tenant_id,
            user_id=user_id,
            permissions=permissions
            if permissions is not None
            else frozenset({"campus.chat.use", "campus.nav"}),
        )
        budget = TurnBudget(
            max_decisions=self._max_decisions,
            max_tool_calls=self._max_tool_calls,
            max_retrievals=self._max_retrievals,
            max_tokens=self._token_budget,
        )
        evidence: list[ChatEvidence] = []
        seen_chunk_ids: set[str] = set()
        yielded_any_delta = False
        first_token_ms: int | None = None

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        spans: list[TraceSpan] = []

        def finish_trace(
            *, stop_reason: StopReason | None, error_code: str | None, tokens_in: int, tokens_out: int
        ) -> None:
            """回合收尾 trace（含 budget span——D5）。"""

            recorder.record_span(
                TraceSpan(
                    trace_id=resolved_trace,
                    tenant_id=tenant_id,
                    span_type="budget",
                    name="budget",
                    start_ms=elapsed_ms(),
                    duration_ms=0,
                    attributes=budget.snapshot(),
                    status="ok",
                )
            )
            recorder.record_turn(
                TraceRecord(
                    trace_id=resolved_trace,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation,
                    turn_id=turn_id,
                    intents=[intent.name for intent in route.route.intents],
                    route_path="react",
                    prompt_name="react",
                    prompt_version="v1",
                    prompt_hash=self._registry.get("react", "v1").sha256,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    first_token_ms=first_token_ms or 0,
                    latency_ms=elapsed_ms(),
                    stop_reason=stop_reason,
                    error_code=error_code,
                    spans=list(spans),
                )
            )

        def event(kind: AgentEventType, payload: dict[str, object]) -> AgentEvent:
            return AgentEvent(type=kind, payload=payload)

        def stage_span(name: str, started_at_ms: int, ok: bool) -> None:
            span = TraceSpan(
                trace_id=resolved_trace,
                tenant_id=tenant_id,
                span_type=name,
                name=name,
                start_ms=started_at_ms,
                duration_ms=elapsed_ms() - started_at_ms,
                status="ok" if ok else "error",
            )
            spans.append(span)
            recorder.record_span(span)

        def merge_evidence(new_items: list[ChatEvidence]) -> None:
            """证据合并（chunk_id 去重；citations 全局编号）。"""

            for item in new_items:
                if item.chunk_id not in seen_chunk_ids:
                    seen_chunk_ids.add(item.chunk_id)
                    evidence.append(item)
            state.set_evidence(evidence)

        def navigate_evidence(target: str, data: dict[str, object]) -> ChatEvidence:
            """navigate 结果 → 入口证据（GENERATE 可引用 route/label）。"""

            return ChatEvidence(
                chunk_id=f"nav:{target}",
                score=1.0,
                text=f"{data.get('label', '')}：{data.get('route', '')}",
                doc_title="业务入口",
                section_path=None,
                page_start=None,
                page_end=None,
                chunk_type="navigate",
                version_id="",
            )

        try:
            if not skip_route_stage:
                yield event(
                    AgentEventType.META,
                    {
                        "turn_id": str(turn_id),
                        "trace_id": resolved_trace,
                        "conversation_id": str(conversation),
                    },
                )
                yield event(
                    AgentEventType.STEP, {"stage": "route", "label": "正在理解问题"}
                )
            intent_count = len(route.route.intents)
            yield event(
                AgentEventType.REASONING_SUMMARY,
                {
                    "summary": (
                        f"识别到 {intent_count} 个问题，正在逐一处理"
                        if intent_count > 1
                        else "正在处理你的问题"
                    )
                },
            )
            llm = self._llm_port
            if llm is None:
                # LLM 未配置：THINK 无法进行 → error 兜底（AI-01a D6 口径）
                yield event(
                    AgentEventType.ERROR,
                    {
                        "code": "LLM_NOT_CONFIGURED",
                        "message": LLM_UNCONFIGURED_TEXT,
                        "retryable": False,
                        "partial": False,
                    },
                )
                await self._flush_audit()
                await self._flush_guards()
                finish_trace(
                    stop_reason=StopReason.CAPABILITY_FALLBACK,
                    error_code="LLM_NOT_CONFIGURED",
                    tokens_in=0,
                    tokens_out=0,
                )
                return

            # ---- 有界 THINK→ACT→OBSERVE 循环（卡 3-A/5-A）----
            messages: list[LlmMessage] = [
                LlmMessage(
                    role="user",
                    content=self._registry.render(
                        REACT_THINK_PROMPT,
                        PROMPT_VERSION,
                        intents_block=_intents_block(route),
                    ),
                )
            ]
            tool_specs = openai_tool_specs(registry)
            async with asyncio.timeout(self._turn_timeout_s):
                while budget.can_decide():
                    yield event(
                        AgentEventType.STEP, {"stage": "think", "label": "正在思考"}
                    )
                    budget.record_decision()
                    decision = await llm.complete(
                        LlmRequest(messages=messages, tool_specs=tool_specs)
                    )
                    if decision.tokens_in is not None:
                        budget.record_tokens(decision.tokens_in + (decision.tokens_out or 0))
                    calls = decision.tool_calls
                    if not calls:
                        break  # 模型不再调工具 → 思考结束
                    observe_parts: list[str] = []
                    for call in calls:
                        if not budget.can_act():
                            observe_parts.append(
                                json.dumps(
                                    {"note": "工具调用预算已耗尽", "tool": "skipped"},
                                    ensure_ascii=False,
                                )
                            )
                            break
                        name = str(call.get("function", {}).get("name", ""))
                        try:
                            args = json.loads(call.get("function", {}).get("arguments") or "{}")
                            if not isinstance(args, dict):
                                args = {}
                        except json.JSONDecodeError:
                            args = {}
                        yield event(
                            AgentEventType.TOOL_CALL,
                            {"tool": name, "status": "running"},
                        )
                        result = await executor.execute(
                            ToolCall(tool=name, args=args, trace_id=resolved_trace), ctx
                        )
                        budget.record_tool_call()
                        result_payload: dict[str, object] = {
                            "tool": name,
                            "status": "ok" if result.ok else "error",
                            "duration_ms": result.duration_ms,
                        }
                        if name == KB_SEARCH and result.ok and isinstance(result.data, dict):
                            hits = result.data.get("hits", [])
                            result_payload["hits"] = len(hits)
                            if budget.can_retrieve():
                                budget.record_retrieval()
                                loaded = list(
                                    await asyncio.wait_for(
                                        self._retriever.load_texts(tenant_id, hits),
                                        timeout=self._retrieve_timeout_s,
                                    )
                                )
                                merge_evidence(loaded)
                            else:
                                observe_parts.append(
                                    json.dumps({"note": "检索预算已耗尽"}, ensure_ascii=False)
                                )
                        elif name == CAMPUS_NAVIGATE and result.ok and isinstance(result.data, dict):
                            target = str(args.get("target", ""))
                            merge_evidence([navigate_evidence(target, result.data)])
                        observe_parts.append(
                            json.dumps(
                                {
                                    "tool": name,
                                    "ok": result.ok,
                                    "summary": result.redacted_summary,
                                    "result": result.data if result.ok else None,
                                    "error_code": result.error_code,
                                },
                                ensure_ascii=False,
                                default=str,
                            )
                        )
                        yield event(AgentEventType.TOOL_RESULT, result_payload)
                    messages.append(
                        LlmMessage(
                            role="user",
                            content="[观察] " + " | ".join(observe_parts),
                        )
                    )
                    yield event(
                        AgentEventType.REASONING_SUMMARY,
                        {"summary": f"已收集 {len(evidence)} 条资料，继续分析"},
                    )
                    if not budget.can_act():
                        # 工具预算耗尽：继续决策无意义 → 带已有证据 GENERATE（卡 5-A）
                        break
                # ---- GENERATE（合并作答，卡 4-A）----
                generate_started = elapsed_ms()
                yield event(
                    AgentEventType.STEP, {"stage": "generate", "label": "正在组织回答"}
                )
                if not evidence:
                    yield event(
                        AgentEventType.DELTA, {"text": FALLBACK_TEXT}
                    )
                    yield event(
                        AgentEventType.DONE,
                        {
                            "turn_id": str(turn_id),
                            "trace_id": resolved_trace,
                            "tokens_in": 0,
                            "tokens_out": 0,
                            "first_token_ms": elapsed_ms(),
                            "latency_ms": elapsed_ms(),
                            "stop_reason": StopReason.CAPABILITY_FALLBACK.value,
                        },
                    )
                    await self._flush_audit()
                    await self._flush_guards()
                    finish_trace(
                        stop_reason=StopReason.CAPABILITY_FALLBACK,
                        error_code=None,
                        tokens_in=0,
                        tokens_out=0,
                    )
                    return
                answer_prompt = self._registry.render(
                    REACT_ANSWER_PROMPT,
                    PROMPT_VERSION,
                    question=message,
                    intents_block=_intents_block(route),
                    evidence_block=format_evidence(evidence),
                )
                stream = llm.stream(
                    LlmRequest(messages=[LlmMessage(role="user", content=answer_prompt)])
                )
                first: LlmStreamEvent = await stream.__anext__()
            # timeout 块外：delta 后按客户端节奏
            first_token_ms = elapsed_ms()
            tokens_in: int | None = None
            tokens_out: int | None = None
            # 输出哨兵（AI-09 卡 1-A；与快路径同款——VERIFY 升级改写，TD-332 收口）
            sentinel = StreamSentinel(evidence_count=len(evidence))
            guard_terminated = False
            guard_hits: list[GuardResultContract] = []
            async for llm_event in self._stream_with_first(first, stream):
                if llm_event.kind == "delta":
                    if not yielded_any_delta:
                        first_token_ms = elapsed_ms()
                    chunk = sentinel.feed(llm_event.text_delta or "")
                    if chunk.results:
                        guard_hits.extend(chunk.results)
                    if chunk.terminated:
                        guard_terminated = True
                        yield event(
                            AgentEventType.DELTA,
                            {"text": chunk.replacement_text or ""},
                        )
                        break
                    if chunk.text:
                        yielded_any_delta = True
                        yield event(AgentEventType.DELTA, {"text": chunk.text})
                elif llm_event.kind == "finish":
                    stage_span("generate", generate_started, ok=True)
                elif llm_event.kind == "usage":
                    tokens_in = llm_event.tokens_in
                    tokens_out = llm_event.tokens_out
            tail = sentinel.flush_final()
            if tail.results:
                guard_hits.extend(tail.results)
            if tail.terminated:
                # M2：尾片 blocking 命中（flush_final 终止）——同 feed 期处置
                yield event(
                    AgentEventType.DELTA,
                    {"text": tail.replacement_text or ""},
                )
            if guard_hits:
                for hit in guard_hits:
                    record_guard_hit(hit.rule, hit.severity, "output")
                if self._guard_events_sink is not None:
                    self._guard_events_sink.append(
                        tenant_id=tenant_id, trace_id=resolved_trace, direction="output",
                        results=guard_hits,
                    )
            if guard_terminated:
                yield event(
                    AgentEventType.DONE,
                    {
                        "turn_id": str(turn_id),
                        "trace_id": resolved_trace,
                        "tokens_in": tokens_in if tokens_in is not None else 0,
                        "tokens_out": tokens_out if tokens_out is not None else 0,
                        "first_token_ms": first_token_ms or 0,
                        "latency_ms": elapsed_ms(),
                        "stop_reason": StopReason.GUARD_BLOCK.value,
                    },
                )
                await self._flush_audit()
                await self._flush_guards()
                finish_trace(
                    stop_reason=StopReason.GUARD_BLOCK, error_code=None,
                    tokens_in=tokens_in or 0, tokens_out=tokens_out or 0,
                )
                return
            if tail.text:
                yielded_any_delta = True
                yield event(AgentEventType.DELTA, {"text": tail.text})
            for index, item in enumerate(evidence, start=1):
                yield event(
                    AgentEventType.CITATION,
                    {
                        "index": index,
                        "chunk_id": item.chunk_id,
                        "document": item.doc_title,
                        "page": item.page_start,
                        "section": item.section_path,
                    },
                )
            yield event(
                AgentEventType.DONE,
                {
                    "turn_id": str(turn_id),
                    "trace_id": resolved_trace,
                    "tokens_in": tokens_in if tokens_in is not None else 0,
                    "tokens_out": tokens_out if tokens_out is not None else 0,
                    "first_token_ms": first_token_ms or 0,
                    "latency_ms": elapsed_ms(),
                    "stop_reason": StopReason.FINAL_ANSWER.value,
                },
            )
            await self._flush_audit()
            await self._flush_guards()
            finish_trace(
                stop_reason=StopReason.FINAL_ANSWER,
                error_code=None,
                tokens_in=tokens_in or 0,
                tokens_out=tokens_out or 0,
            )
        except (GeneratorExit, asyncio.CancelledError):
            finish_trace(
                stop_reason=StopReason.USER_CANCELLED, error_code=None, tokens_in=0, tokens_out=0
            )
            raise
        except LlmError as exc:
            yield event(
                AgentEventType.ERROR,
                {
                    "code": exc.code.value,
                    "message": "回答生成失败，请重试",
                    "retryable": exc.retryable,
                    "partial": yielded_any_delta,
                },
            )
            await self._flush_audit()
            await self._flush_guards()
            finish_trace(
                stop_reason=None, error_code=exc.code.value, tokens_in=0, tokens_out=0
            )
        except TimeoutError:
            yield event(
                AgentEventType.ERROR,
                {
                    "code": "LLM_TIMEOUT",
                    "message": "回答生成超时，请重试",
                    "retryable": True,
                    "partial": yielded_any_delta,
                },
            )
            await self._flush_audit()
            await self._flush_guards()
            finish_trace(
                stop_reason=StopReason.TIMEOUT, error_code="LLM_TIMEOUT", tokens_in=0, tokens_out=0
            )
        except Exception:
            logger.exception("chat ReAct 回合内部错误")
            yield event(
                AgentEventType.ERROR,
                {
                    "code": "STREAM_ERROR",
                    "message": "回答生成失败，请重试",
                    "retryable": True,
                    "partial": yielded_any_delta,
                },
            )
            await self._flush_audit()
            await self._flush_guards()
            finish_trace(stop_reason=None, error_code="STREAM_ERROR", tokens_in=0, tokens_out=0)

    async def _stream_with_first(
        self, first: LlmStreamEvent, stream: AsyncIterator[LlmStreamEvent]
    ) -> AsyncIterator[LlmStreamEvent]:
        """先产出超时段内取到的首事件，再继续余下流。"""

        yield first
        async for llm_event in stream:
            yield llm_event

    async def _flush_guards(self) -> None:
        """M4：ReAct 路径护栏事件落库（与快路径 flush_guards 同款）。"""

        flush = getattr(self._guard_events_sink, "flush", None)
        if flush is not None:
            await flush()

    async def _flush_audit(self) -> None:
        """回合收尾审计落库（取消路径不 flush——TD-018 口径）。"""

        flush = getattr(self._audit_sink, "flush", None)
        if flush is not None:
            await flush()
