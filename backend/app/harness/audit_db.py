"""harness 审计端口的 DB 适配器（M4-AI01b 步 2，简报 D3）。

`ToolExecutor` 的 `AuditSink` 协议是**同步** `append`；DB 写入是异步——采用
「append 缓冲 + `flush()` 落库」：loop 在回合收尾前调用 `flush()`，把本轮
`tool.execute` 事件一次性写入审计表（每事件独立短事务）。

口径：chat 工具全部只读，审计为留痕而非写操作门禁——单事件落库失败记
warning 不中断回合（尽力落库）；缓冲在 flush 后清空，失败事件不重试（避免
重放放大）。
"""

import logging

from app.audit.writer import append_audit
from app.db.session import db_session
from app.harness.audit import AuditEvent

logger = logging.getLogger(__name__)


class DbAuditSink:
    """`AuditSink` 的 DB 实装（actor_realm 固定 tenant——chat 只在租户侧）。"""

    def __init__(self, *, actor_realm: str = "tenant") -> None:
        self._pending: list[AuditEvent] = []
        self._actor_realm = actor_realm

    def append(self, event: AuditEvent) -> AuditEvent:
        """缓冲事件（同步协议）；落库发生在 flush。"""

        self._pending.append(event)
        return event

    def pending_count(self) -> int:
        """当前缓冲事件数（测试与诊断用）。"""

        return len(self._pending)

    async def flush(self) -> int:
        """落库全部缓冲事件；返回成功条数。单事件失败记 warning 并丢弃。"""

        events, self._pending = self._pending, []
        written = 0
        for event in events:
            metadata = dict(event.metadata)
            if event.trace_id:
                metadata.setdefault("trace_id", event.trace_id)
            # harness 词表 result="error" ↔ audit_logs CHECK 词表 "failure"（适配器职责）
            result = "failure" if event.result == "error" else event.result
            try:
                async with db_session(tenant_id=event.tenant_id) as session:
                    await append_audit(
                        session,
                        action=event.action,
                        resource_type=event.resource_type,
                        actor_realm=self._actor_realm,
                        resource_id=event.resource_id,
                        actor_id=event.actor_id,
                        tenant_id=event.tenant_id,
                        result=result,
                        metadata=metadata,
                    )
                written += 1
            except Exception as exc:  # noqa: BLE001——尽力落库：失败不中断回合
                logger.warning(
                    "chat 工具审计落库失败 action=%s：%s.%s",
                    event.action,
                    type(exc).__module__,
                    type(exc).__name__,
                )
        return written
