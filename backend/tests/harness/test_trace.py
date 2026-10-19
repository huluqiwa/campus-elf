"""Trace 记录测试。"""

from uuid import uuid4

import pytest

from app.harness.contracts import StopReason, TraceRecord, TraceSpan
from app.harness.trace import InMemoryTraceSink, TraceRecorder


def test_trace_recorder_writes_turn_and_span() -> None:
    """TraceRecorder 将回合与 span 写入 sink，并脱敏 attributes。"""

    sink = InMemoryTraceSink()
    recorder = TraceRecorder(sink)
    tenant_id = uuid4()
    user_id = uuid4()
    conversation_id = uuid4()
    turn_id = uuid4()

    span = TraceSpan(
        trace_id="trace-1",
        tenant_id=tenant_id,
        span_type="retrieve",
        name="kb.search",
        start_ms=100,
        duration_ms=20,
        attributes={"top_k": 10, "token": "secret-token", "email": "a@example.com"},
    )
    record = TraceRecord(
        trace_id="trace-1",
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        intents=["kb_qa"],
        route_path="fast",
        model="test-model",
        tokens_in=10,
        tokens_out=5,
        first_token_ms=800,
        latency_ms=1200,
        stop_reason=StopReason.FINAL_ANSWER,
        spans=[span],
    )

    recorder.record_span(span)
    recorder.record_turn(record)

    assert len(sink.spans) == 1
    assert sink.spans[0].attributes["top_k"] == 10
    assert sink.spans[0].attributes["token"] == "***"
    assert sink.spans[0].attributes["email"] == "***"
    assert sink.turns[0].trace_id == "trace-1"
    assert sink.turns[0].spans[0].attributes["token"] == "***"
    assert sink.turns[0].spans[0].attributes["email"] == "***"

def test_in_memory_sink_capacity_trims_oldest() -> None:
    """容量满后按 FIFO 丢弃最旧记录（避免长驻进程无界增长）。"""

    sink = InMemoryTraceSink(capacity=2)
    for index in range(3):
        sink.write_span(
            TraceSpan(
                trace_id=f"t{index}",
                span_type="stage",
                name=f"s{index}",
                start_ms=0,
                duration_ms=0,
            )
        )

    assert [span.trace_id for span in sink.spans] == ["t1", "t2"]


def test_in_memory_sink_rejects_non_positive_capacity() -> None:
    """非法容量直接报错，避免静默退化为无界。"""

    with pytest.raises(ValueError, match="capacity must be positive"):
        InMemoryTraceSink(capacity=0)
