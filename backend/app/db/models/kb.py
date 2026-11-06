"""知识库上传模型（M3 KB-01）：批次 / 文档 / 文档版本。

口径（`docs/design-briefs/M3-KB-01-upload.md`，已确认）：
- 一次上传 = 1 条 `kb_upload_batches` + N 条文档 + N 条版本，**整批原子**；
- `(tenant_id, idempotency_key)` 唯一，重试返回原批次；
- 版本状态只在本点使用 `UPLOADED`（扫描中/待重试）、`PENDING_APPROVAL`（扫描通过）、
  `SCAN_FAILED`（感染或完整性失败）；文档状态与当前版本保持一致；
- 对象 key 由服务端按 `kb/<tenant_id>/<document_id>/<version_id>/source<ext>` 生成，
  数据库不存租户外路径。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

# KB-02 起版本/文档状态机扩展到审批结论（APPROVED/REJECTED）；
# 构建后续状态（BUILDING/READY/…）随 KB-03 迁移放宽。
KB_STATUSES = ("UPLOADED", "PENDING_APPROVAL", "SCAN_FAILED", "APPROVED", "REJECTED")
_STATUS_SQL = ", ".join(f"'{status}'" for status in KB_STATUSES)


class KbUploadBatch(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """一次批量上传（幂等键与请求指纹）。"""

    __tablename__ = "kb_upload_batches"
    __table_args__ = (
        CheckConstraint(
            "file_count > 0 AND file_count <= 15",
            name="file_count_range",
        ),
        CheckConstraint("total_bytes > 0", name="total_bytes_positive"),
        UniqueConstraint("id", "tenant_id", name="uq_kb_upload_batches_id_tenant_id"),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_kb_upload_batches_tenant_idempotency_key",
        ),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_upload_batches_created_by_users",
            ondelete="CASCADE",
        ),
    )

    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # 请求指纹：按顺序的 `filename|size|sha256`，同键换文件时用于 409
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)


class KbDocument(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """知识库文档（元数据 + 当前版本指针）。"""

    __tablename__ = "kb_documents"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_SQL})", name="status_valid"),
        CheckConstraint(
            "upload_file_index >= 0", name="upload_file_index_nonnegative"
        ),
        UniqueConstraint("id", "tenant_id", name="uq_kb_documents_id_tenant_id"),
        UniqueConstraint(
            "tenant_id",
            "upload_batch_id",
            "upload_file_index",
            name="uq_kb_documents_tenant_upload_batch_index",
        ),
        Index("ix_kb_documents_tenant_created_at_id", "tenant_id", "created_at", "id"),
        ForeignKeyConstraint(
            ["upload_batch_id", "tenant_id"],
            ["kb_upload_batches.id", "kb_upload_batches.tenant_id"],
            name="fk_kb_documents_upload_batch_id_kb_upload_batches",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_documents_created_by_users",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["current_version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_documents_current_version_id_kb_document_versions",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    tags: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="UPLOADED")
    current_version_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    upload_batch_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    upload_file_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)


class KbDocumentVersion(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """文档版本（源文件 + 扫描结论）；解析/构建字段在后续点新增。"""

    __tablename__ = "kb_document_versions"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_STATUS_SQL})", name="status_valid"
        ),
        CheckConstraint("version_no >= 1", name="version_no_positive"),
        CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
        CheckConstraint(
            "scan_attempts >= 0", name="scan_attempts_nonnegative"
        ),
        UniqueConstraint("id", "tenant_id", name="uq_kb_document_versions_id_tenant_id"),
        UniqueConstraint(
            "tenant_id",
            "document_id",
            "version_no",
            name="uq_kb_document_versions_tenant_document_version",
        ),
        Index("ix_kb_document_versions_document_id", "document_id"),
        Index("ix_kb_document_versions_tenant_status_updated", "tenant_id", "status", "updated_at"),
        ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["kb_documents.id", "kb_documents.tenant_id"],
            name="fk_kb_document_versions_document_id_kb_documents",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_document_versions_created_by_users",
            ondelete="CASCADE",
        ),
    )

    document_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_object_key: Mapped[str] = mapped_column(String(300), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mime: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="UPLOADED")
    scan_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    scan_error_code: Mapped[str | None] = mapped_column(String(32))
    scan_signature: Mapped[str | None] = mapped_column(String(200))
    # `clamav`（真实扫描）/ `dev-skip`（开发跳过）/ `dev-degraded`（开发放行）
    scan_engine: Mapped[str | None] = mapped_column(String(32))
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)


BUILD_JOB_STATUSES = ("BUILD_QUEUED",)
_BUILD_JOB_STATUS_SQL = ", ".join(f"'{status}'" for status in BUILD_JOB_STATUSES)


class KbBuildJob(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """构建任务（M3 KB-02 只到创建门禁 + BUILD_QUEUED；进度/执行器/重试随 KB-03）。

    同一版本最多一个活跃任务由部分唯一索引兜底
    （`(tenant_id, version_id) WHERE status IN ('BUILD_QUEUED','BUILDING')`；
    `BUILDING` 尚不在 CHECK 内，但索引进来可以避免 KB-03 放宽状态时重建索引）。
    """

    __tablename__ = "kb_build_jobs"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_BUILD_JOB_STATUS_SQL})", name="status_valid"
        ),
        UniqueConstraint("id", "tenant_id", name="uq_kb_build_jobs_id_tenant_id"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_build_jobs_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_build_jobs_version_id_kb_document_versions",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_build_jobs_created_by_users",
            ondelete="CASCADE",
        ),
        Index(
            "uq_kb_build_jobs_tenant_version_active",
            "tenant_id",
            "version_id",
            unique=True,
            postgresql_where=text("status IN ('BUILD_QUEUED', 'BUILDING')"),
        ),
        Index("ix_kb_build_jobs_tenant_status_updated", "tenant_id", "status", "updated_at"),
    )

    version_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="BUILD_QUEUED")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_by: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
