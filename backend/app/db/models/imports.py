"""导入任务模型（M2 ORG-02）：import_jobs / import_row_errors。

口径（`docs/design-briefs/M2-ORG02-import.md`，已确认）：
- job 是**租户级**数据（上传者所在校），行级原子：失败行进 `import_row_errors`，
  合法行照常入库（不产生半成品**行**）；
- `row_filter_json` 仅重试 job 携带（源 job 失败行号集合）；`retried_by` 由源 job 指向派生 job；
- `raw_json` 保存原始行数据（重试免二次解析整文件）。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

ImportType = str  # 'teachers' | 'students'
ImportConflict = str  # 'error' | 'skip'
ImportStatus = str  # 'pending' | 'running' | 'completed' | 'completed_with_errors' | 'failed'


class ImportJob(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """一次导入任务（上传 → 入队 → worker 逐行执行）。"""

    __tablename__ = "import_jobs"
    __table_args__ = (
        CheckConstraint("type IN ('teachers', 'students')", name="ck_import_jobs_type_valid"),
        CheckConstraint("conflict IN ('error', 'skip')", name="ck_import_jobs_conflict_valid"),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'completed_with_errors', 'failed')",
            name="ck_import_jobs_status_valid",
        ),
        CheckConstraint(
            "total_rows >= 0 AND success_rows >= 0 AND failed_rows >= 0 AND skipped_rows >= 0",
            name="ck_import_jobs_counters_nonnegative",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_import_jobs_id_tenant_id"),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_import_jobs_created_by_users",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_import_jobs_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
    )

    type: Mapped[str] = mapped_column(String(16), nullable=False)
    conflict: Mapped[str] = mapped_column(String(8), nullable=False, server_default="error")
    file_object_key: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="pending")
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    success_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    skipped_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_report_key: Mapped[str | None] = mapped_column(String(300))
    # 重试 job 携带：源 job 的失败行号集合（JSON 数组）；普通 job 为 NULL
    row_filter_json: Mapped[Any | None] = mapped_column(JSONB)
    # 源 job 指向派生 job（重试后回填）；派生 job 本行为 NULL
    retried_by: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_by: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message: Mapped[str | None] = mapped_column(Text)


class ImportRowError(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """单条错误行（行号 + 字段 + 原因 + 原始行数据）。"""

    __tablename__ = "import_row_errors"
    __table_args__ = (
        Index("ix_import_row_errors_job_id", "job_id"),
        ForeignKeyConstraint(
            ["job_id", "tenant_id"],
            ["import_jobs.id", "import_jobs.tenant_id"],
            name="fk_import_row_errors_job_id_import_jobs",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_import_row_errors_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
    )

    job_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[Any] = mapped_column(JSONB, nullable=False)
