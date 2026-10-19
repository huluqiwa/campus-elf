"""本地 trace 记录。

M0-C 使用内存 sink；写入 span 前统一做敏感字段脱敏。
"""

from typing import Protocol

from app.harness.audit import redact_mapping
from app.harness.contracts import TraceRecord, TraceSpan


class TraceSink(Protocol):
    """trace 写入端口。"""

    def write_turn(self, record: TraceRecord) -> None:
        """写入一个回合 trace。"""

    def write_span(self, span: TraceSpan) -> None:
        """写入一个 span。"""


class InMemoryTraceSink:
    """测试/开发用内存 sink（有界，避免长驻进程无界增长）。"""

    def __init__(self, capacity: int = 1000) -> None:
        """初始化空 sink。

        参数:
            capacity: 最多保留的回合/span 数；超出后丢弃最旧记录。

        异常:
            ValueError: capacity <= 0。
        """

        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self.turns: list[TraceRecord] = []
        self.spans: list[TraceSpan] = []

    def _trim(self, items: list[TraceRecord] | list[TraceSpan]) -> None:
        """按容量裁剪最旧记录。"""

        overflow = len(items) - self._capacity
        if overflow > 0:
            del items[:overflow]

    def write_turn(self, record: TraceRecord) -> None:
        """保存回合 trace 深拷贝，并脱敏嵌套 span attributes。"""

        safe_spans = [
            span.model_copy(update={"attributes": redact_mapping(span.attributes)})
            for span in record.spans
        ]
        self.turns.append(record.model_copy(update={"spans": safe_spans}, deep=True))
        self._trim(self.turns)

    def write_span(self, span: TraceSpan) -> None:
        """保存 span，并脱敏 attributes。"""

        safe_span = span.model_copy(
            update={"attributes": redact_mapping(span.attributes)}
        )
        self.spans.append(safe_span)
        self._trim(self.spans)


class TraceRecorder:
    """trace 记录门面。"""

    def __init__(self, sink: TraceSink) -> None:
        """绑定 trace sink。"""

        self._sink = sink

    def record_turn(self, record: TraceRecord) -> None:
        """记录回合 trace。"""

        self._sink.write_turn(record)

    def record_span(self, span: TraceSpan) -> None:
        """记录 span。"""

        self._sink.write_span(span)
