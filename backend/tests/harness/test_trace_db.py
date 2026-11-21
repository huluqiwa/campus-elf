"""DbTraceSink 单元测试：缓冲 + flush 落库编排（save_trace 打桩，无 DB）。

覆盖：turn 内嵌 span 与独立上报 span 的去重（loop 段双通道）；单条落库失败
记 warning 不中断（尽力落库口径）；flush 后缓冲清空；生产缺省装配断言。
"""

from uuid import uuid4

import pytest

from app.harness.contracts import TraceRecord, TraceSpan
from app.harness.trace_db import DbTraceSink


def _record(*, spans: list[TraceSpan] | None = None) -> TraceRecord:
    return TraceRecord(
        trace_id="trace-1",
        tenant_id=uuid4(),
        user_id=uuid4(),
        conversation_id=uuid4(),
        turn_id=uuid4(),
        route_path="fast",
        provider="fake",
        model="fake-model",
        spans=spans or [],
    )


def _span(trace_id: str = "trace-1", name: str = "generate") -> TraceSpan:
    return TraceSpan(
        trace_id=trace_id, tenant_id=uuid4(), span_type=name, name=name,
        start_ms=0, duration_ms=5,
    )


class _RecorderCalls:
    """打桩调用记录（类型化；fail_on = "turn"/"span" 注入失败弧）。"""

    def __init__(self) -> None:
        self.turns: list[TraceRecord] = []
        self.spans: list[TraceSpan] = []
        self.fail_on = ""


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> _RecorderCalls:
    """打桩 persistence 层：记录调用并支持注入失败。"""

    import app.harness.trace_db as trace_db

    recorded = _RecorderCalls()

    async def fake_save_trace(*, tenant_id: object, record: TraceRecord) -> None:
        if recorded.fail_on == "turn":
            raise RuntimeError("boom")
        recorded.turns.append(record)

    async def fake_save_span(*, tenant_id: object, record: TraceSpan) -> None:
        if recorded.fail_on == "span":
            raise RuntimeError("boom")
        recorded.spans.append(record)

    monkeypatch.setattr(trace_db, "save_trace", fake_save_trace)
    monkeypatch.setattr(trace_db, "save_trace_span", fake_save_span)
    return recorded


@pytest.mark.asyncio
async def test_flush_dedupes_embedded_spans_and_writes_usage_fields(calls: _RecorderCalls) -> None:
    """turn 内嵌 span 不重复落库；独立 span（未内嵌）补写；字段透传。"""

    sink = DbTraceSink()
    embedded = _span("trace-1", "generate")
    standalone = _span("trace-1", "tool.execute")
    sink.write_turn(_record(spans=[embedded]))
    sink.write_span(embedded)  # loop 段双通道：与内嵌同 span → flush 去重
    sink.write_span(standalone)  # ToolExecutor 独立 span → 补写

    written = await sink.flush()
    assert written == 2  # 1 turn + 1 独立 span（内嵌重复被去重）
    assert len(calls.turns) == 1
    assert calls.turns[0].provider == "fake"
    assert [s.name for s in calls.spans] == ["tool.execute"]
    assert sink.pending_count() == 0


@pytest.mark.asyncio
async def test_flush_failure_does_not_raise_and_clears_buffer(calls: _RecorderCalls) -> None:
    """turn 落库失败 → warning 丢弃不重试（不中断回合）；缓冲清空；span 不受影响。"""

    sink = DbTraceSink()
    sink.write_turn(_record())
    sink.write_span(_span("trace-1", "generate"))
    calls.fail_on = "turn"
    written = await sink.flush()
    assert written == 1  # turn 失败被吞，独立 span 照常落库
    assert len(calls.spans) == 1
    assert sink.pending_count() == 0

    calls.fail_on = "span"
    sink.write_span(_span("trace-2", "generate"))
    assert await sink.flush() == 0
    assert sink.pending_count() == 0


def test_production_default_trace_sink_is_db_sink() -> None:
    """生产缺省装配 = DbTraceSink（第 2 轮审查 Blocker-1：TD-330 在生产可达）。

    无 DB 依赖：只断言装配产物类型，不触发 flush。
    """

    from app.main import create_app

    app = create_app()
    assert isinstance(app.state.trace_sink, DbTraceSink)
