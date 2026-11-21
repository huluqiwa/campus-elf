"""trace 端口的 DB 适配器（M4-AI06/07，TD-330 收口；DbAuditSink 同款缓冲模式）。

`TraceSink` 协议是**同步** `write_turn`/`write_span`；DB 写入是异步——采用
「缓冲 + `flush()` 落库」：orchestrator 在回合事件消费完毕后调用 `flush()`，
把本轮 trace 一次性写入 chat_traces/chat_trace_spans/llm_usage。

口径：trace/usage 为可观测性留痕而非业务正确性依赖——单条落库失败记 warning
不中断回合（尽力落库）；缓冲在 flush 后清空，失败记录不重试（避免重放放大）。

span 去重：loop 段 span 既内嵌在 record.spans 又经 record_span 独立上报
（`stream_stage_span` 双通道）；flush 按 (trace_id, span_type, name, start_ms)
去重，只补写未内嵌的独立 span（如 ToolExecutor 上报的工具 span）。
"""

import logging

from app.harness.contracts import TraceRecord, TraceSpan
from app.harness.persistence import save_trace, save_trace_span

logger = logging.getLogger(__name__)


class DbTraceSink:
    """`TraceSink` 的 DB 实装（TD-330：生产装配用；InMemory 保留为测试替身）。"""

    def __init__(self) -> None:
        self._pending_turns: list[TraceRecord] = []
        self._pending_spans: list[TraceSpan] = []

    def write_turn(self, record: TraceRecord) -> None:
        """缓冲回合 trace（同步协议）；落库发生在 flush。"""

        self._pending_turns.append(record)

    def write_span(self, span: TraceSpan) -> None:
        """缓冲独立 span（同步协议）；落库发生在 flush。"""

        self._pending_spans.append(span)

    def pending_count(self) -> int:
        """当前缓冲条数（测试与诊断用）。"""

        return len(self._pending_turns) + len(self._pending_spans)

    async def flush(self) -> int:
        """落库全部缓冲记录；返回成功条数。单条失败记 warning 并丢弃。"""

        turns, self._pending_turns = self._pending_turns, []
        spans, self._pending_spans = self._pending_spans, []
        written = 0
        embedded_keys: set[tuple[str, str, str, int]] = set()
        for record in turns:
            try:
                await save_trace(tenant_id=record.tenant_id, record=record)
                written += 1
                embedded_keys |= {
                    (s.trace_id, s.span_type, s.name, s.start_ms) for s in record.spans
                }
            except Exception as exc:  # noqa: BLE001——尽力落库：失败不中断回合
                logger.warning(
                    "chat trace 落库失败 turn_id=%s：%s.%s",
                    record.turn_id,
                    type(exc).__module__,
                    type(exc).__name__,
                )
        for span in spans:
            if span.tenant_id is None:
                logger.warning("trace span 缺 tenant_id，跳过落库 trace_id=%s", span.trace_id)
                continue
            key = (span.trace_id, span.span_type, span.name, span.start_ms)
            if key in embedded_keys:
                continue  # 已随 turn 内嵌落库（loop 段 span 双通道去重）
            try:
                await save_trace_span(tenant_id=span.tenant_id, record=span)
                written += 1
            except Exception as exc:  # noqa: BLE001——尽力落库：失败不中断回合
                logger.warning(
                    "chat trace span 落库失败 trace_id=%s：%s.%s",
                    span.trace_id,
                    type(exc).__module__,
                    type(exc).__name__,
                )
        return written
