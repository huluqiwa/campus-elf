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
    """测试/开发用内存 sink。"""

    def __init__(self) -> None:
        """初始化空 sink。"""

        self.turns: list[TraceRecord] = []
        self.spans: list[TraceSpan] = []

    def write_turn(self, record: TraceRecord) -> None:
        """保存回合 trace 深拷贝，并脱敏嵌套 span attributes。"""

        safe_spans = [
            span.model_copy(update={"attributes": redact_mapping(span.attributes)})
            for span in record.spans
        ]
        self.turns.append(record.model_copy(update={"spans": safe_spans}, deep=True))

    def write_span(self, span: TraceSpan) -> None:
        """保存 span，并脱敏 attributes。"""

        safe_span = span.model_copy(
            update={"attributes": redact_mapping(span.attributes)}
        )
        self.spans.append(safe_span)


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
