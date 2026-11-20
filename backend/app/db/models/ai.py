"""AI 域模型（M4-AI09：guard_events；design §4.4 AI、会话、反馈表族的第一个落地表）。

会话/消息/反馈/trace 表随 AI-06/07 与会话持久化点建表；本文件先承载护栏事件。
"""

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class GuardEvent(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """护栏事件（M4-AI09；design §4.4 `guard_events`，仅追加——不授 update/delete）。

    `excerpt_redacted` 为脱敏摘要（D3：命中本体以 `[REDACTED:<rule>]` 占位，
    事件表不存原始攻击载荷全文）。
    """

    __tablename__ = "guard_events"
    __table_args__ = (
        CheckConstraint(
            "direction IN ('input', 'output')", name="ck_guard_events_direction"
        ),
        CheckConstraint(
            "severity IN ('low', 'medium', 'high')", name="ck_guard_events_severity"
        ),
        CheckConstraint(
            "action IN ('allow', 'block', 'redact', 'fallback')",
            name="ck_guard_events_action",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_guard_events_id_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_guard_events_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        Index("ix_guard_events_tenant_trace", "tenant_id", "trace_id"),
        Index("ix_guard_events_tenant_created", "tenant_id", "created_at"),
        {
            "comment": "AI 输入/输出护栏事件（M4-AI09；仅追加）",
        },
    )

    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    rule: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    excerpt_redacted: Mapped[str] = mapped_column(Text, nullable=False)
