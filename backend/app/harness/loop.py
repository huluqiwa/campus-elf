"""快路径 Loop（M4-AI01b 步 4）：显式状态机 + SSE 事件产出 + 兜底链。

六段管道（design §6.2）：输入轻检（契约已限长）→ ROUTE（规则直通单意图，
卡 5-A）→ RETRIEVE（kb.search 工具 + 证据文本回查）→ GENERATE（LLM 流式）→
VERIFY（引用序号一致性——超范围引用**告警不改写**：delta 已发不可撤回，全量
输出护栏归 AI-09）→ 输出轻检（占位，AI-09）→ DONE。

事件契约（design §5.8）：meta → step(route/retrieve) → tool_call → tool_result
→ reasoning_summary → delta* → citation* → done；error 语义（retryable/partial）
见各分支。

预算（简报 D5，审查 M3 后修正表述）：`TURN_TIMEOUT` 硬超时只包 **LLM 建流+首
事件**；RETRIEVE 段由工具级 5s 超时（kb.search）+ 证据回查 `wait_for(5s)` 覆盖
——三层各管一段；delta 开始后流式按客户端节奏消费；LlmPort 自带连接/首 token
超时与熔断（AI-01a）。

trace：每个已开始回合必发（空壳先例延续）；审计 `flush` 在正常/失败路径收尾前
调用（客户端断开路径不 flush——取消上下文内写库需 shield，TD-018 口径）。
"""

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from app.core.metrics import record_guard_hit
from app.harness.audit import AuditSink
from app.harness.chat_tools import KB_SEARCH, TurnToolState, build_turn_tools
from app.harness.contracts import (
    AgentEvent,
    AgentEventType,
    GuardResult,
    StopReason,
    TenantContext,
    ToolCall,
    TraceRecord,
    TraceSpan,
)
from app.harness.guards.stream_guard import StreamSentinel
from app.harness.llm import LlmError, LlmMessage, LlmPort, LlmRequest, LlmStreamEvent
from app.harness.prompts import PromptRegistry
from app.harness.tools import ToolExecutor
from app.harness.trace import TraceRecorder, TraceSink
from app.kb.chat_retrieval import ChatEvidence, ChatRetriever

logger = logging.getLogger(__name__)

PROMPT_NAME = "kb_qa"
PROMPT_VERSION = "v1"

# 兜底话术（固定文案；不提示「存在但无权」——design §5.8）
CAPABILITY_FALLBACK_TEXT = (
    "抱歉，我暂时没有在学校知识库中找到与这个问题相关的内容。"
    "你可以换个说法再试一次，或联系管理员确认相关资料是否已发布。"
)
RETRIEVAL_FAILED_TEXT = "知识库检索暂时不可用，请稍后重试。"
LLM_UNCONFIGURED_TEXT = "AI 回答能力尚未配置，请联系管理员。"

_REF_PATTERN = re.compile(r"\[(\d+)\]")


def format_evidence(evidence: list[ChatEvidence]) -> str:
    """证据块拼装：[n]（文档 · 章节 · 页码）+ 文本（页码缺省省略）。

    快路径与 ReAct 共用（引用编号全局一致）。
    """

    blocks: list[str] = []
    for index, item in enumerate(evidence, start=1):
        source = item.doc_title or "未命名文档"
        if item.section_path:
            source += f" · {item.section_path}"
        if item.page_start:
            source += f" · 第{item.page_start}页"
        blocks.append(f"[{index}]（{source}）\n{item.text}")
    return "\n\n".join(blocks)


class FastPathLoop:
    """快路径编排器（use-site 构造；端口注入对齐 AI-01a 纪律）。

    参数:
        llm_port: LLM 端口；None = 未配置（对话走 LLM_NOT_CONFIGURED 兜底）。
        retriever: chat 检索器（kb.search 工具面 + 证据文本回查）。
        prompt_registry: 生产 Prompt 注册表（Git 版本化模板）。
        trace_sink: 本地 trace sink（InMemory；落库随会话持久化点）。
        audit_sink: 审计端口（DbAuditSink——append 缓冲 + flush 落库）。
        turn_timeout_s: 首 token 前关键段硬超时（秒）。
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
    ) -> None:
        self._llm_port = llm_port
        self._retriever = retriever
        self._prompt_registry = prompt_registry
        self._trace_sink = trace_sink
        self._audit_sink = audit_sink
        self._guard_events_sink = guard_events_sink
        self._turn_timeout_s = turn_timeout_s
        self._retrieve_timeout_s = retrieve_timeout_s

    async def run(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        turn_id: UUID,
        message: str,
        conversation_id: UUID | None = None,
        permissions: frozenset[str] | None = None,
        skip_route_stage: bool = False,
        trace_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """执行一个快路径回合；产出 SSE 事件（类型化 AgentEvent）。

        参数:
            permissions: principal 实际权限集（TD-334——ctx 从实际权限派生）；
                None 仅限测试/兼容，生产路径由 orchestrator 必传。
            skip_route_stage: True = orchestrator 已发 meta/step(route)（本 loop 跳过——D1）。
            trace_id: orchestrator 传入的回合 trace id（None 时自行生成）。
        """

        trace_id_resolved = trace_id if trace_id is not None else uuid4().hex
        conversation = conversation_id if conversation_id is not None else uuid4()
        started = time.perf_counter()
        recorder = TraceRecorder(self._trace_sink)
        state = TurnToolState()
        registry = build_turn_tools(retriever=self._retriever, state=state)
        executor = ToolExecutor(registry, trace=recorder, audit=self._audit_sink)
        # 工具权限（TD-334）：从 principal 实际权限派生（orchestrator 必传）；
        # None 仅测试/兼容路径沿用同码集。data_scope 均 "none"，无需范围集。
        ctx = TenantContext(
            tenant_id=tenant_id,
            user_id=user_id,
            permissions=permissions
            if permissions is not None
            else frozenset({"campus.chat.use", "campus.nav"}),
        )
        evidence: list[ChatEvidence] = []
        yielded_any_delta = False
        first_token_ms: int | None = None

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        spans: list[TraceSpan] = []

        def finish_trace(
            *, stop_reason: StopReason | None, error_code: str | None, tokens_in: int, tokens_out: int
        ) -> None:
            """回合收尾 trace（正常/失败/取消路径共用；空壳先例「必发」）。

            段 span 随 record 内嵌（对齐空壳先例形状）；工具 span 由 ToolExecutor
            经 recorder 独立写入。
            """

            recorder.record_turn(
                TraceRecord(
                    trace_id=trace_id_resolved,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation,
                    turn_id=turn_id,
                    intents=["kb_qa"],
                    route_path="fast",
                    prompt_name=PROMPT_NAME,
                    prompt_version=PROMPT_VERSION,
                    prompt_hash=self._prompt_hash(),
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

        def record_guard(direction: str, results: list[GuardResult]) -> None:
            """护栏命中：metrics + guard_events 缓冲（A5）。"""

            for result in results:
                record_guard_hit(result.rule, result.severity, direction)
            if self._guard_events_sink is not None and results:
                self._guard_events_sink.append(
                    tenant_id=tenant_id,
                    trace_id=trace_id_resolved,
                    direction=direction,
                    results=results,
                )

        async def flush_guards() -> None:
            flush = getattr(self._guard_events_sink, "flush", None)
            if flush is not None:
                await flush()

        def stream_stage_span(name: str, started_at_ms: int, ok: bool) -> None:
            span = TraceSpan(
                trace_id=trace_id_resolved,
                tenant_id=tenant_id,
                span_type=name,
                name=name,
                start_ms=started_at_ms,
                duration_ms=elapsed_ms() - started_at_ms,
                status="ok" if ok else "error",
            )
            spans.append(span)
            recorder.record_span(span)

        try:
            if not skip_route_stage:
                yield event(
                    AgentEventType.META,
                    {
                        "turn_id": str(turn_id),
                        "trace_id": trace_id_resolved,
                        "conversation_id": str(conversation),
                    },
                )
                yield event(
                    AgentEventType.STEP, {"stage": "route", "label": "正在理解问题"}
                )
            retrieve_started = elapsed_ms()
            yield event(
                AgentEventType.STEP,
                {"stage": "retrieve", "label": "正在检索学校知识库"},
            )
            yield event(
                AgentEventType.TOOL_CALL, {"tool": KB_SEARCH, "status": "running"}
            )
            result = await executor.execute(
                ToolCall(tool=KB_SEARCH, args={"query": message}, trace_id=trace_id_resolved), ctx
            )
            if not result.ok or not isinstance(result.data, dict):
                yield event(
                    AgentEventType.TOOL_RESULT,
                    {
                        "tool": KB_SEARCH,
                        "status": "error",
                        "duration_ms": result.duration_ms,
                    },
                )
                yield event(
                    AgentEventType.ERROR,
                    {
                        "code": "RETRIEVAL_FAILED",
                        "message": RETRIEVAL_FAILED_TEXT,
                        "retryable": True,
                        "partial": False,
                    },
                )
                stream_stage_span("retrieve", retrieve_started, ok=False)
                await self._flush_audit()
                await flush_guards()
                finish_trace(stop_reason=None, error_code="RETRIEVAL_FAILED", tokens_in=0, tokens_out=0)
                return
            hits = result.data.get("hits", [])
            yield event(
                AgentEventType.TOOL_RESULT,
                {
                    "tool": KB_SEARCH,
                    "status": "ok",
                    "duration_ms": result.duration_ms,
                    "hits": len(hits),
                },
            )
            try:
                evidence = list(
                    await asyncio.wait_for(
                        self._retriever.load_texts(tenant_id, hits),
                        timeout=self._retrieve_timeout_s,
                    )
                )
            except TimeoutError:
                yield event(
                    AgentEventType.ERROR,
                    {
                        "code": "RETRIEVAL_FAILED",
                        "message": RETRIEVAL_FAILED_TEXT,
                        "retryable": True,
                        "partial": False,
                    },
                )
                stream_stage_span("retrieve", retrieve_started, ok=False)
                await self._flush_audit()
                await flush_guards()
                finish_trace(
                    stop_reason=None, error_code="RETRIEVAL_FAILED", tokens_in=0, tokens_out=0
                )
                return
            state.set_evidence(evidence)
            if not evidence:
                # 零命中（含人群空集 fail-closed）：兜底话术，不提示「存在但无权」
                stream_stage_span("retrieve", retrieve_started, ok=True)
                yield event(
                    AgentEventType.REASONING_SUMMARY,
                    {"summary": "没有找到相关资料"},
                )
                yield event(AgentEventType.DELTA, {"text": CAPABILITY_FALLBACK_TEXT})
                yield event(
                    AgentEventType.DONE,
                    {
                        "turn_id": str(turn_id),
                        "trace_id": trace_id_resolved,
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "first_token_ms": elapsed_ms(),
                        "latency_ms": elapsed_ms(),
                        "stop_reason": StopReason.CAPABILITY_FALLBACK.value,
                    },
                )
                await self._flush_audit()
                await flush_guards()
                finish_trace(
                    stop_reason=StopReason.CAPABILITY_FALLBACK, error_code=None,
                    tokens_in=0, tokens_out=0,
                )
                return
            yield event(
                AgentEventType.REASONING_SUMMARY,
                {"summary": f"已找到 {len(evidence)} 条相关资料，正在核对引用"},
            )
            if self._llm_port is None:
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
                await flush_guards()
                finish_trace(
                    stop_reason=StopReason.CAPABILITY_FALLBACK,
                    error_code="LLM_NOT_CONFIGURED",
                    tokens_in=0,
                    tokens_out=0,
                )
                return

            generate_started = elapsed_ms()
            prompt = self._prompt_registry.render(
                PROMPT_NAME,
                PROMPT_VERSION,
                question=message,
                evidence=self._format_evidence(evidence),
            )
            request = LlmRequest(messages=[LlmMessage(role="user", content=prompt)])
            stream = self._llm_port.stream(request)
            tokens_in: int | None = None
            tokens_out: int | None = None
            sentinel = StreamSentinel(evidence_count=len(evidence))
            guard_terminated = False
            try:
                # 首 token 前 30s 硬超时（D5：关键段；delta 后流式按客户端节奏）。
                async with asyncio.timeout(self._turn_timeout_s):
                    first: LlmStreamEvent = await stream.__anext__()
                # first_token_ms 口径=回合起点（含 route/retrieve——m2 审查修正：
                # P95 门禁按完整回合计，两路径口径统一）
                first_token_ms = elapsed_ms()
                async for llm_event in self._stream_with_first(first, stream):
                    if llm_event.kind == "delta":
                        if not yielded_any_delta:
                            first_token_ms = elapsed_ms()
                        chunk = sentinel.feed(llm_event.text_delta or "")
                        if chunk.results:
                            record_guard("output", chunk.results)
                        if chunk.terminated:
                            guard_terminated = True
                            yield event(
                                AgentEventType.DELTA,
                                {"text": chunk.replacement_text or ""},
                            )
                            break
                        if chunk.text:
                            yielded_any_delta = True
                            yield event(
                                AgentEventType.DELTA, {"text": chunk.text}
                            )
                    elif llm_event.kind == "finish":
                        stream_stage_span("generate", generate_started, ok=True)
                    elif llm_event.kind == "usage":
                        tokens_in = llm_event.tokens_in
                        tokens_out = llm_event.tokens_out
            except LlmError as exc:
                yield event(
                    AgentEventType.ERROR,
                    {
                        "code": exc.code.value,
                        "message": self._llm_safe_text(exc.code.value),
                        "retryable": exc.retryable,
                        "partial": yielded_any_delta,
                    },
                )
                stream_stage_span("generate", generate_started, ok=False)
                await self._flush_audit()
                await flush_guards()
                finish_trace(
                    stop_reason=None, error_code=exc.code.value, tokens_in=tokens_in or 0,
                    tokens_out=tokens_out or 0,
                )
                return
            except TimeoutError:
                yield event(
                    AgentEventType.ERROR,
                    {
                        "code": "LLM_TIMEOUT",
                        "message": self._llm_safe_text("LLM_TIMEOUT"),
                        "retryable": True,
                        "partial": yielded_any_delta,
                    },
                )
                stream_stage_span("generate", generate_started, ok=False)
                await self._flush_audit()
                await flush_guards()
                finish_trace(
                    stop_reason=StopReason.TIMEOUT, error_code="LLM_TIMEOUT",
                    tokens_in=tokens_in or 0, tokens_out=tokens_out or 0,
                )
                return

            tail = sentinel.flush_final()
            if tail.results:
                record_guard("output", tail.results)
            if guard_terminated:
                yield event(
                    AgentEventType.DONE,
                    {
                        "turn_id": str(turn_id),
                        "trace_id": trace_id_resolved,
                        "tokens_in": tokens_in if tokens_in is not None else 0,
                        "tokens_out": tokens_out if tokens_out is not None else 0,
                        "first_token_ms": first_token_ms or 0,
                        "latency_ms": elapsed_ms(),
                        "stop_reason": StopReason.GUARD_BLOCK.value,
                    },
                )
                await self._flush_audit()
                await flush_guards()
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
                    "trace_id": trace_id_resolved,
                    # usage 未报时计 0（不伪造 provider 报数；A7 红线同源纪律）
                    "tokens_in": tokens_in if tokens_in is not None else 0,
                    "tokens_out": tokens_out if tokens_out is not None else 0,
                    "first_token_ms": first_token_ms or 0,
                    "latency_ms": elapsed_ms(),
                    "stop_reason": StopReason.FINAL_ANSWER.value,
                },
            )
            await self._flush_audit()
            await flush_guards()
            finish_trace(
                stop_reason=StopReason.FINAL_ANSWER, error_code=None,
                tokens_in=tokens_in or 0, tokens_out=tokens_out or 0,
            )
        except (GeneratorExit, asyncio.CancelledError):
            # 客户端断开/任务取消：trace 必发（USER_CANCELLED）；取消上下文内不 flush
            # 审计（写库需 shield——TD-018 口径）。
            finish_trace(
                stop_reason=StopReason.USER_CANCELLED, error_code=None,
                tokens_in=0, tokens_out=0,
            )
            raise
        except Exception:
            logger.exception("chat 快路径回合内部错误")
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
            await flush_guards()
            finish_trace(stop_reason=None, error_code="STREAM_ERROR", tokens_in=0, tokens_out=0)

    async def _stream_with_first(
        self, first: LlmStreamEvent, stream: AsyncIterator[LlmStreamEvent]
    ) -> AsyncIterator[LlmStreamEvent]:
        """先产出超时段内取到的首事件，再继续余下流。"""

        yield first
        async for llm_event in stream:
            yield llm_event

    def _prompt_hash(self) -> str:
        """当前生产 prompt 的 sha256（trace 元数据）。"""

        spec = self._prompt_registry.get(PROMPT_NAME, PROMPT_VERSION)
        return spec.sha256

    def _format_evidence(self, evidence: list[ChatEvidence]) -> str:
        """证据块拼装：[n]（文档 · 章节 · 页码）+ 文本（页码缺省省略）。"""

        blocks: list[str] = []
        for index, item in enumerate(evidence, start=1):
            source = item.doc_title or "未命名文档"
            if item.section_path:
                source += f" · {item.section_path}"
            if item.page_start:
                source += f" · 第{item.page_start}页"
            blocks.append(f"[{index}]（{source}）\n{item.text}")
        return "\n\n".join(blocks)

    def _verify_references(self, answer_parts: list[str], evidence_count: int) -> None:
        """VERIFY：引用序号一致性检测（超出条目数 → 告警不改写）。

        delta 已流式发出、不可撤回；本防线只记录 guard 告警供 AI-09 全量护栏
        接管前的观测（简报 §10 预判 TD-P3 的过渡口径）。
        """

        answer = "".join(answer_parts)
        out_of_range = [
            int(match) for match in _REF_PATTERN.findall(answer) if int(match) > evidence_count
        ]
        if out_of_range:
            logger.warning(
                "LLM_GUARD_REF_OUT_OF_RANGE refs=%s evidence=%s", out_of_range, evidence_count
            )

    def _llm_safe_text(self, code: str) -> str:
        """error 事件话术（固定安全文案映射；不透传 provider 细节）。"""

        return {
            "LLM_TIMEOUT": "回答生成超时，请重试",
            "LLM_BREAKER_OPEN": "AI 服务暂时不可用，请稍后重试",
            "LLM_RATE_LIMITED": "提问过于频繁，请稍后重试",
            "LLM_AUTH": "AI 服务鉴权失败，请联系管理员",
            "LLM_NOT_CONFIGURED": LLM_UNCONFIGURED_TEXT,
        }.get(code, "回答生成失败，请重试")

    async def _flush_audit(self) -> None:
        """回合收尾审计落库（DbAuditSink.flush；失败已在 sink 内记 warning）。"""

        flush = getattr(self._audit_sink, "flush", None)
        if flush is not None:
            await flush()
