"""护栏事件落库适配（M4-AI09 步 4；DbAuditSink 同款缓冲+flush）。

loop 在每个命中点 `append`（纯内存缓冲），回合收尾 `flush()` 落库——单事件
独立短事务；`excerpt_redacted` 由检测器产出（D3 脱敏口径），此处不再二次处理。
"""

import logging
import uuid

from app.db.session import db_session
from app.harness.contracts import GuardResult

logger = logging.getLogger(__name__)


class GuardEventsDbSink:
    """guard_events 落库端口（仅追加；方向由 loop 标注 input/output）。"""

    def __init__(self) -> None:
        self._pending: list[tuple[uuid.UUID, str, str, GuardResult]] = []

    def append(
        self,
        *,
        tenant_id: uuid.UUID,
        trace_id: str,
        direction: str,
        results: list[GuardResult],
    ) -> None:
        """缓冲一批命中（同步；flush 落库）。"""

        for result in results:
            self._pending.append((tenant_id, trace_id, direction, result))

    def pending_count(self) -> int:
        """当前缓冲条数（测试/诊断）。"""

        return len(self._pending)

    async def flush(self) -> int:
        """落库全部缓冲事件；返回成功条数。单事件失败记 warning 并丢弃。"""

        events, self._pending = self._pending, []
        written = 0
        for tenant_id, trace_id, direction, result in events:
            try:
                from app.db.models.ai import GuardEvent

                async with db_session(tenant_id=tenant_id) as session:
                    session.add(
                        GuardEvent(
                            tenant_id=tenant_id,
                            trace_id=trace_id[:64],
                            direction=direction,
                            rule=result.rule,
                            severity=result.severity,
                            action=result.action,
                            excerpt_redacted=result.reason,
                        )
                    )
                    await session.commit()
                written += 1
            except Exception as exc:  # noqa: BLE001——尽力落库：失败不中断回合
                logger.warning(
                    "护栏事件落库失败 rule=%s：%s.%s",
                    result.rule,
                    type(exc).__module__,
                    type(exc).__name__,
                )
        return written
