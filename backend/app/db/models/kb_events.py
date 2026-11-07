"""构建事件模型（M3 KB-03B）：管线阶段/进度/终态的持久化（SSE 回放基底）。"""

from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

BUILD_EVENT_TYPES = ("build.stage", "build.progress", "build.done", "build.error")
_EVENT_TYPE_SQL = ", ".join(f"'{t}'" for t in BUILD_EVENT_TYPES)


class KbBuildEvent(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """构建事件（`docs/design.md` §5.6；seq 按 job 递增，SSE 回放/续传基底）。"""

    __tablename__ = "kb_build_events"
    __table_args__ = (
        CheckConstraint(f"event_type IN ({_EVENT_TYPE_SQL})", name="event_type_valid"),
        CheckConstraint("seq >= 1", name="seq_positive"),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_build_events_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["job_id", "tenant_id"],
            ["kb_build_jobs.id", "kb_build_jobs.tenant_id"],
            name="fk_kb_build_events_job_id_kb_build_jobs",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_kb_build_events_id_tenant_id"),
        UniqueConstraint("job_id", "seq", name="uq_kb_build_events_job_seq"),
        Index("ix_kb_build_events_tenant_id", "tenant_id"),
        Index("ix_kb_build_events_job_seq", "job_id", "seq", unique=True),
    )

    job_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
