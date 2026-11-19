"""FastPathLoop 状态机校验（M4-AI01b 步 4）。

事件序（design §5.8）、兜底链（零命中/LLM 未配置/LLM 失败）、引用事件与证据
同源、trace 必发、审计 flush 必调。检索用 stub、LLM 用 FakeLlm（AI-01a 基建）。
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast
from uuid import uuid4

import pytest

from app.harness.audit import AuditEvent, AuditSink
from app.harness.chat_tools import KB_SEARCH
from app.harness.llm import LlmError, LlmErrorCode, LlmPort, LlmStreamEvent
from app.harness.loop import (
    CAPABILITY_FALLBACK_TEXT,
    FastPathLoop,
)
from app.harness.prompt_loading import build_default_registry
from app.harness.trace import InMemoryTraceSink
from app.kb.chat_retrieval import ChatEvidence, ChatRetrievalError, ChatRetriever
from app.kb.embedding import TEIEmbedder
from tests.support.llm import FakeLlm


class StubEmbedder(TEIEmbedder):
    """无网络 embedder（继承真实类以通过类型面）。"""

    def __init__(self) -> None:
        super().__init__(base_url="http://127.0.0.1:1", timeout_seconds=1, batch_size=1)

    async def embed(self, texts: Any) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


class StubRetriever(ChatRetriever):
    """检索 stub：search_hits 返回预置元数据；load_texts 返回预置证据。"""

    def __init__(
        self,
        *,
        hits: list[dict[str, Any]] | Exception | None = None,
        evidence: list[ChatEvidence] | None = None,
    ) -> None:
        super().__init__(embedder=StubEmbedder(), qdrant_url=None)
        self._hits = hits if hits is not None else []
        self._evidence = evidence
        self.load_calls = 0

    async def search_hits(self, *, tenant_id: Any, user_id: Any, query: str) -> list[dict[str, Any]]:
        if isinstance(self._hits, Exception):
            raise self._hits
        return self._hits

    async def load_texts(self, tenant_id: Any, hits: Sequence[dict[str, Any]]) -> list[ChatEvidence]:
        self.load_calls += 1
        return self._evidence if self._evidence is not None else []


class StubAuditSink(AuditSink):
    """审计 stub：记录 flush 次数（loop 必调口径）。"""

    def __init__(self) -> None:
        self.flushed = 0

    def append(self, event: AuditEvent) -> AuditEvent:
        return event

    async def flush(self) -> int:
        self.flushed += 1
        return 0


def evidence(chunk_id: str, text: str, title: str = "校历") -> ChatEvidence:
    """构造一条证据。"""

    return ChatEvidence(
        chunk_id=chunk_id,
        score=0.9,
        text=text,
        doc_title=title,
        section_path="第一章",
        page_start=3,
        page_end=3,
        chunk_type="child",
        version_id="v1",
    )


def hit_meta(chunk_id: str) -> dict[str, object]:
    """检索命中（真实 `search_points` 形状：payload 嵌套）。"""

    return {
        "chunk_id": chunk_id,
        "score": 0.9,
        "payload": {
            "doc_title": "校历",
            "section_path": "第一章",
            "page_start": 3,
            "page_end": 3,
            "chunk_type": "child",
            "version_id": "v1",
        },
    }


def build_loop(
    llm_port: object | None,
    retriever: StubRetriever,
    *,
    audit: StubAuditSink | None = None,
) -> tuple[FastPathLoop, InMemoryTraceSink, StubAuditSink]:
    """构造被测 loop（内存 trace sink 供断言）。"""

    sink = InMemoryTraceSink()
    audit_sink = audit if audit is not None else StubAuditSink()
    registry = build_default_registry()
    loop = FastPathLoop(
        llm_port=cast("LlmPort | None", llm_port),
        retriever=retriever,
        prompt_registry=registry,
        trace_sink=sink,
        audit_sink=audit_sink,
    )
    return loop, sink, audit_sink


async def collect(aiter: AsyncIterator[Any]) -> list[Any]:
    """收集事件流（消费即驱动生成器）。"""

    return [event async for event in aiter]


def types_of(events: list[Any]) -> list[str]:
    return [event.type.value for event in events]


async def test_happy_path_event_order_and_citations() -> None:
    """成功路径：事件序与载荷对齐 design §5.8；引用与证据同源；usage 进 done。"""

    retriever = StubRetriever(
        hits=[hit_meta("c1"), hit_meta("c2")],
        evidence=[evidence("c1", "请假需班主任审批。"), evidence("c2", "三天内提交。")],
    )
    fake = FakeLlm(
        stream_scripts=[
            [
                LlmStreamEvent(kind="delta", text_delta="请假需班主任审批 [1]。"),
                LlmStreamEvent(kind="delta", text_delta="三天内提交 [2]。"),
                LlmStreamEvent(kind="finish", finish_reason="stop"),
                LlmStreamEvent(kind="usage", tokens_in=120, tokens_out=30),
            ]
        ]
    )
    loop, sink, audit = build_loop(fake, retriever)
    turn_id = uuid4()
    events = await collect(
        loop.run(tenant_id=uuid4(), user_id=uuid4(), turn_id=turn_id, message="请假流程")
    )

    assert types_of(events) == [
        "meta",
        "step",
        "step",
        "tool_call",
        "tool_result",
        "reasoning_summary",
        "delta",
        "delta",
        "citation",
        "citation",
        "done",
    ]
    meta = events[0].payload
    assert meta["turn_id"] == str(turn_id)
    assert meta["conversation_id"]
    tool_result = events[4].payload
    assert tool_result["tool"] == KB_SEARCH
    assert tool_result["status"] == "ok"
    assert tool_result["hits"] == 2
    citations = [event.payload for event in events if event.type.value == "citation"]
    assert [item["index"] for item in citations] == [1, 2]
    assert citations[0]["chunk_id"] == "c1"
    assert citations[0]["document"] == "校历"
    assert citations[0]["section"] == "第一章"
    assert citations[0]["page"] == 3
    done = events[-1].payload
    assert done["stop_reason"] == "FINAL_ANSWER"
    assert done["tokens_in"] == 120
    assert done["tokens_out"] == 30
    assert done["first_token_ms"] >= 0
    assert audit.flushed == 1
    records = sink.turns
    assert records[-1].stop_reason is not None
    assert records[-1].stop_reason.value == "FINAL_ANSWER"


async def test_zero_hits_falls_back_without_llm() -> None:
    """检索零命中：兜底话术 delta + done(CAPABILITY_FALLBACK)；LLM 零调用。"""

    retriever = StubRetriever(hits=[])
    fake = FakeLlm()
    loop, _sink, _audit = build_loop(fake, retriever)
    events = await collect(
        loop.run(tenant_id=uuid4(), user_id=uuid4(), turn_id=uuid4(), message="校历")
    )

    assert types_of(events)[-1] == "done"
    done = events[-1].payload
    assert done["stop_reason"] == "CAPABILITY_FALLBACK"
    assert done["tokens_out"] == 0
    deltas = [event.payload["text"] for event in events if event.type.value == "delta"]
    assert deltas == [CAPABILITY_FALLBACK_TEXT]
    assert fake.requests == []  # LLM 零调用


async def test_llm_not_configured_yields_error_without_done() -> None:
    """LLM 未配置：tool_result 正常 → error(LLM_NOT_CONFIGURED)；无 done；trace CAPABILITY_FALLBACK。"""

    retriever = StubRetriever(
        hits=[hit_meta("c1")], evidence=[evidence("c1", "内容。")]
    )
    loop, sink, _audit = build_loop(None, retriever)
    events = await collect(
        loop.run(tenant_id=uuid4(), user_id=uuid4(), turn_id=uuid4(), message="校历")
    )

    assert types_of(events)[-1] == "error"
    error = events[-1].payload
    assert error["code"] == "LLM_NOT_CONFIGURED"
    assert error["retryable"] is False
    assert error["partial"] is False
    assert all(event.type.value != "done" for event in events)
    assert sink.turns[-1].stop_reason is not None
    assert sink.turns[-1].stop_reason.value == "CAPABILITY_FALLBACK"


async def test_llm_failure_before_first_delta() -> None:
    """LLM 失败（未产出 delta）：error partial=False、retryable 按端口标记。"""

    retriever = StubRetriever(
        hits=[hit_meta("c1")], evidence=[evidence("c1", "内容。")]
    )
    fake = FakeLlm(
        stream_scripts=[[LlmError(LlmErrorCode.TIMEOUT, retryable=True)]]
    )
    loop, _sink, _audit = build_loop(fake, retriever)
    events = await collect(
        loop.run(tenant_id=uuid4(), user_id=uuid4(), turn_id=uuid4(), message="校历")
    )

    assert types_of(events)[-1] == "error"
    error = events[-1].payload
    assert error["code"] == "LLM_TIMEOUT"
    assert error["retryable"] is True
    assert error["partial"] is False


async def test_llm_failure_after_delta_marks_partial() -> None:
    """流中断（已产出 delta）：error partial=True（客户端保留已完成部分）。"""

    retriever = StubRetriever(
        hits=[hit_meta("c1")], evidence=[evidence("c1", "内容。")]
    )
    fake = FakeLlm(
        stream_scripts=[
            [
                LlmStreamEvent(kind="delta", text_delta="部分回答"),
                LlmError(LlmErrorCode.PROVIDER_ERROR, retryable=False),
            ]
        ]
    )
    loop, _sink, _audit = build_loop(fake, retriever)
    events = await collect(
        loop.run(tenant_id=uuid4(), user_id=uuid4(), turn_id=uuid4(), message="校历")
    )

    error = events[-1].payload
    assert events[-1].type.value == "error"
    assert error["code"] == "LLM_PROVIDER_ERROR"
    assert error["partial"] is True
    assert error["retryable"] is False


async def test_retrieval_infra_failure_yields_retryable_error() -> None:
    """检索基础设施故障：error(RETRIEVAL_FAILED, retryable=True)；不调 LLM。"""

    retriever = StubRetriever(hits=ChatRetrievalError("CHAT_SEARCH_FAILED"))
    fake = FakeLlm()
    loop, _sink, _audit = build_loop(fake, retriever)
    events = await collect(
        loop.run(tenant_id=uuid4(), user_id=uuid4(), turn_id=uuid4(), message="校历")
    )

    error = events[-1].payload
    assert events[-1].type.value == "error"
    assert error["code"] == "RETRIEVAL_FAILED"
    assert error["retryable"] is True
    assert fake.requests == []


async def test_out_of_range_reference_is_flagged_not_crashing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """答案引用超范围：不崩溃、done 正常；guard 告警记录（AI-09 全量护栏前最小防线）。"""

    retriever = StubRetriever(
        hits=[hit_meta("c1")], evidence=[evidence("c1", "内容。")]
    )
    fake = FakeLlm(
        stream_scripts=[
            [
                LlmStreamEvent(kind="delta", text_delta="回答 [1]，另见 [5] 不存在引用。"),
                LlmStreamEvent(kind="finish", finish_reason="stop"),
            ]
        ]
    )
    loop, _sink, _audit = build_loop(fake, retriever)
    with caplog.at_level(logging.WARNING, logger="app.harness.loop"):
        events = await collect(
            loop.run(tenant_id=uuid4(), user_id=uuid4(), turn_id=uuid4(), message="校历")
        )

    assert types_of(events)[-1] == "done"
    assert "LLM_GUARD_REF_OUT_OF_RANGE" in caplog.text


async def test_trace_recorded_with_prompt_metadata() -> None:
    """trace 必发：prompt 三元组/intents/route 落 record（内存 sink 断言）。"""

    retriever = StubRetriever(
        hits=[hit_meta("c1")], evidence=[evidence("c1", "内容。")]
    )
    fake = FakeLlm(
        stream_scripts=[[LlmStreamEvent(kind="finish", finish_reason="stop")]]
    )
    loop, sink, _audit = build_loop(fake, retriever)
    await collect(
        loop.run(tenant_id=uuid4(), user_id=uuid4(), turn_id=uuid4(), message="校历")
    )

    record = sink.turns[-1]
    assert record.route_path == "fast"
    assert record.intents == ["kb_qa"]
    assert record.prompt_name == "kb_qa"
    assert record.prompt_version == "v1"
    assert record.prompt_hash
    assert record.tokens_in == 0  # 该脚本未报 usage：trace 如实计 0（不伪造口径）


async def test_client_disconnect_records_user_cancelled() -> None:
    """取消弧回归（审查 M1）：客户端断开（aclose）→ trace USER_CANCELLED 必发。"""

    retriever = StubRetriever(hits=[hit_meta("c1")], evidence=[evidence("c1", "内容。")])
    fake = FakeLlm(
        stream_scripts=[
            [
                LlmStreamEvent(kind="delta", text_delta="部分"),
                LlmStreamEvent(kind="delta", text_delta="剩余"),
                LlmStreamEvent(kind="finish", finish_reason="stop"),
            ]
        ]
    )
    loop, sink, audit = build_loop(fake, retriever)
    generator = loop.run(tenant_id=uuid4(), user_id=uuid4(), turn_id=uuid4(), message="校历")
    # async generator 才有 aclose（run 声明为 AsyncIterator——运行时是生成器）
    assert hasattr(generator, "aclose")
    consumed: list[Any] = []
    async for event in generator:
        consumed.append(event)
        if event.type.value == "delta":
            break  # 消费方在首个 delta 后断开
    await generator.aclose()

    assert consumed[-1].type.value == "delta"
    assert sink.turns[-1].stop_reason is not None
    assert sink.turns[-1].stop_reason.value == "USER_CANCELLED"
    # 取消路径 audit 不 flush（TD-018：取消上下文内写库需 shield）
    assert audit.flushed == 0


class _SlowFirstTokenLlm:
    """首 token 前长挂起的 LLM stub（超时弧注入）。"""

    def __init__(self, delay_s: float) -> None:
        self._delay_s = delay_s
        self.requests: list[Any] = []

    async def complete(self, request: Any) -> Any:
        raise NotImplementedError

    async def stream(self, request: Any) -> AsyncIterator[LlmStreamEvent]:
        self.requests.append(request)
        await asyncio.sleep(self._delay_s)
        yield LlmStreamEvent(kind="delta", text_delta="迟到")
        yield LlmStreamEvent(kind="finish", finish_reason="stop")


async def test_turn_timeout_yields_llm_timeout() -> None:
    """超时弧回归（审查 M2）：首 token 前 30s 预算触发 → error(LLM_TIMEOUT)+trace TIMEOUT。"""

    retriever = StubRetriever(hits=[hit_meta("c1")], evidence=[evidence("c1", "内容。")])
    sink = InMemoryTraceSink()
    audit = StubAuditSink()
    loop = FastPathLoop(
        llm_port=_SlowFirstTokenLlm(delay_s=1.0),
        retriever=retriever,
        prompt_registry=build_default_registry(),
        trace_sink=sink,
        audit_sink=audit,
        turn_timeout_s=0.2,
    )
    events = await collect(
        loop.run(tenant_id=uuid4(), user_id=uuid4(), turn_id=uuid4(), message="校历")
    )

    assert types_of(events)[-1] == "error"
    error = events[-1].payload
    assert error["code"] == "LLM_TIMEOUT"
    assert error["retryable"] is True
    assert error["partial"] is False
    assert all(name != "done" for name in types_of(events))
    assert sink.turns[-1].stop_reason is not None
    assert sink.turns[-1].stop_reason.value == "TIMEOUT"


async def test_load_texts_timeout_yields_retrieval_failed() -> None:
    """回查超时回归（审查 M3）：证据回查无界→wait_for 上限，超时 error(RETRIEVAL_FAILED)。"""

    class _HangingLoadRetriever(StubRetriever):
        async def load_texts(self, tenant_id: Any, hits: Sequence[dict[str, Any]]) -> list[ChatEvidence]:
            await asyncio.sleep(2.0)
            return []

    retriever = _HangingLoadRetriever(hits=[hit_meta("c1")])
    sink = InMemoryTraceSink()
    loop = FastPathLoop(
        llm_port=FakeLlm(),
        retriever=retriever,
        prompt_registry=build_default_registry(),
        trace_sink=sink,
        audit_sink=StubAuditSink(),
        retrieve_timeout_s=0.2,
    )
    events = await collect(
        loop.run(tenant_id=uuid4(), user_id=uuid4(), turn_id=uuid4(), message="校历")
    )

    assert types_of(events)[-1] == "error"
    assert events[-1].payload["code"] == "RETRIEVAL_FAILED"
    assert events[-1].payload["retryable"] is True
