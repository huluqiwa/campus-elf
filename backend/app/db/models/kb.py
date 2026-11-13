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
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
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
# KB-03A 起扩展构建三态（BUILDING/READY/FAILED，`state-machines.md` §3）。
KB_STATUSES = (
    "UPLOADED",
    "PENDING_APPROVAL",
    "SCAN_FAILED",
    "APPROVED",
    "REJECTED",
    "BUILDING",
    "READY",
    "FAILED",
    "TESTING",
    "TEST_PASSED",
    "TEST_FAILED",
    "PUBLISHED",
    "OFFLINE",
    "DELETED",
)
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
        ForeignKeyConstraint(
            ["acl_confirmed_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_document_versions_acl_confirmed_by_users",
            ondelete="SET NULL",
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
    # K4 人群确认态（KB-09A）：null = 未确认（fail-closed 门禁判据，spec :307）
    acl_confirmed_by: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    acl_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)


# 版本级查阅人群类型（spec KB-09 :306；`all` 显式选择、audience_id 为空）
KB_AUDIENCE_TYPES = ("department", "role", "class", "grade", "course", "user", "all")
_AUDIENCE_TYPE_SQL = ", ".join(f"'{t}'" for t in KB_AUDIENCE_TYPES)


class KbDocumentAudience(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """版本级查阅人群（K4；spec :306 多条为并集，`all` 显式选择）。

    `audience_id` VARCHAR 多态引用（无 FK）：UUID 类型存字符串、`grade` 存年级
    字符串（`classes.grade` 为 String(16)）；存在性与同租校验在服务层。唯一
    `(version_id, audience_type, audience_id)` 用 NULLS NOT DISTINCT——防止
    `all` 行（audience_id NULL）重复。
    """

    __tablename__ = "kb_document_audiences"
    __table_args__ = (
        CheckConstraint(f"audience_type IN ({_AUDIENCE_TYPE_SQL})", name="audience_type_valid"),
        UniqueConstraint(
            "version_id",
            "audience_type",
            "audience_id",
            name="uq_kb_document_audiences_version_type_audience",
            postgresql_nulls_not_distinct=True,
        ),
        UniqueConstraint("id", "tenant_id", name="uq_kb_document_audiences_id_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_document_audiences_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_document_audiences_version_id_kb_document_versions",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_document_audiences_created_by_users",
            ondelete="CASCADE",
        ),
        Index("ix_kb_document_audiences_version_id", "version_id"),
        Index("ix_kb_document_audiences_tenant_id", "tenant_id"),
    )

    version_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    audience_type: Mapped[str] = mapped_column(String(24), nullable=False)
    audience_id: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)


BUILD_JOB_STATUSES = ("BUILD_QUEUED", "BUILDING", "READY", "FAILED")
_BUILD_JOB_STATUS_SQL = ", ".join(f"'{status}'" for status in BUILD_JOB_STATUSES)

# job 内阶段（`state-machines.md` §4）：QUEUED 与终态无 stage，执行中必有；终态保留最后阶段。
BUILD_STAGES = ("PARSING", "NORMALIZING", "CHUNKING", "EMBEDDING", "INDEXING", "VALIDATING")
_STAGE_SQL = ", ".join(f"'{stage}'" for stage in BUILD_STAGES)


class KbBuildJob(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """构建任务（M3 KB-03A：状态/阶段/进度/tier/错误全字段 + 重试计数）。

    同一版本最多一个活跃任务由部分唯一索引兜底
    （`(tenant_id, version_id) WHERE status IN ('BUILD_QUEUED','BUILDING')`，
    KB-02 建索引时已把 BUILDING 预留在内）。
    """

    __tablename__ = "kb_build_jobs"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_BUILD_JOB_STATUS_SQL})", name="status_valid"
        ),
        CheckConstraint(f"stage IS NULL OR stage IN ({_STAGE_SQL})", name="stage_valid"),
        CheckConstraint(
            "progress IS NULL OR (progress >= 0 AND progress <= 1)", name="progress_range"
        ),
        CheckConstraint("retry_count >= 0", name="retry_count_nonnegative"),
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
        # 看门狗扫描 stale BUILDING 用（heartbeat_at 超时 → FAILED/STALE）
        Index("ix_kb_build_jobs_tenant_heartbeat", "tenant_id", "status", "heartbeat_at"),
    )

    version_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="BUILD_QUEUED")
    stage: Mapped[str | None] = mapped_column(String(24))
    progress: Mapped[float | None] = mapped_column(Float)
    processed_units: Mapped[int | None] = mapped_column(Integer)
    total_units: Mapped[int | None] = mapped_column(Integer)
    tier: Mapped[str | None] = mapped_column(String(32))
    fallback_reason: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_by: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)


CHUNK_TYPES = ("parent", "child")
_CHUNK_TYPE_SQL = ", ".join(f"'{t}'" for t in CHUNK_TYPES)
EMBEDDING_STATUSES = ("pending", "ready", "failed")
_EMBEDDING_STATUS_SQL = ", ".join(f"'{s}'" for s in EMBEDDING_STATUSES)


class KbChunk(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """分块（M3 KB-03A；父子块，表格/代码整体不拆——`docs/design.md` §7.2）。

    `is_human_edited/edited_by/edited_at` 是 K5（KB-10）的列，建表时一并落
    （可空 + 默认 false），避免后续点再迁移；本点恒为默认值。
    """

    __tablename__ = "kb_chunks"
    __table_args__ = (
        CheckConstraint(f"chunk_type IN ({_CHUNK_TYPE_SQL})", name="chunk_type_valid"),
        UniqueConstraint("id", "tenant_id", name="uq_kb_chunks_id_tenant_id"),
        CheckConstraint(
            f"embedding_status IN ({_EMBEDDING_STATUS_SQL})", name="embedding_status_valid"
        ),
        CheckConstraint("chunk_index >= 0", name="chunk_index_nonnegative"),
        CheckConstraint(
            "token_count >= 0 AND char_count >= 0", name="count_nonnegative"
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_chunks_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_chunks_document_version_id_kb_document_versions",
            ondelete="CASCADE",
        ),
        # 父块自引用（parent_id 为空 = 顶层/父块本身）；循环引用 use_alter 建表后补
        ForeignKeyConstraint(
            ["parent_id", "tenant_id"],
            ["kb_chunks.id", "kb_chunks.tenant_id"],
            name="fk_kb_chunks_parent_id_kb_chunks",
            ondelete="CASCADE",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["edited_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_chunks_edited_by_users",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "document_version_id",
            "chunk_index",
            name="uq_kb_chunks_tenant_version_chunk_index",
        ),
        Index("ix_kb_chunks_tenant_id", "tenant_id"),
        Index("ix_kb_chunks_document_version_id", "document_version_id"),
    )

    document_version_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    chunk_type: Mapped[str] = mapped_column(String(16), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    section_path: Mapped[str | None] = mapped_column(String(500))
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    overlap_prev: Mapped[int | None] = mapped_column(Integer)
    overlap_next: Mapped[int | None] = mapped_column(Integer)
    chunk_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    embedding_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    is_human_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    edited_by: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


INDEX_VERSION_STATUSES = ("building", "ready", "published", "retired")
_INDEX_VERSION_STATUS_SQL = ", ".join(f"'{s}'" for s in INDEX_VERSION_STATUSES)


class KbIndexVersion(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """版本化索引记录（M3 KB-03A：collection 名与构建状态；alias 切换是 KB-05）。

    命名 `tenant_{tenant_hash}_kb_v{version_id}`（`docs/design.md` §7.4）；
    同一版本重复构建复用同一行（唯一 `(tenant_id, document_version_id)`）。
    """

    __tablename__ = "kb_index_versions"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_INDEX_VERSION_STATUS_SQL})", name="status_valid"
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_index_versions_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_index_versions_document_version_id_kb_document_versions",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_kb_index_versions_id_tenant_id"),
        UniqueConstraint(
            "tenant_id",
            "document_version_id",
            name="uq_kb_index_versions_tenant_version",
        ),
        Index("ix_kb_index_versions_tenant_id", "tenant_id"),
    )

    document_version_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    qdrant_collection: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="building")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KbPublishAlias(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """发布别名指针（M3 KB-05；design §4.3 `kb_publish_aliases`，D1-A 每文档一行）。

    `collection`/`version_id` 是当前活跃发布的事实来源（M4 检索解析用）；同名 Qdrant
    named alias 是派生的可观测视图（切换失败可经幂等重放重建）。唯一
    `(tenant_id, document_id)` 兜底并发首发布恰一行。
    """

    __tablename__ = "kb_publish_aliases"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "document_id", name="uq_kb_publish_aliases_tenant_document"
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_publish_aliases_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["kb_documents.id", "kb_documents.tenant_id"],
            name="fk_kb_publish_aliases_document_id_kb_documents",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_publish_aliases_version_id_kb_document_versions",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_kb_publish_aliases_id_tenant_id"),
        Index("ix_kb_publish_aliases_tenant_id", "tenant_id"),
    )

    document_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    alias: Mapped[str] = mapped_column(String(200), nullable=False)
    collection: Mapped[str] = mapped_column(String(200), nullable=False)
    version_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)


TEST_SET_FORMATS = ("csv", "xlsx", "json")
_TEST_SET_FORMAT_SQL = ", ".join(f"'{f}'" for f in TEST_SET_FORMATS)
TEST_RUN_STATUSES = ("TESTING", "TEST_PASSED", "TEST_FAILED", "STALE")
_TEST_RUN_STATUS_SQL = ", ".join(f"'{s}'" for s in TEST_RUN_STATUSES)


class KbTestSet(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """测试集（M3 KB-04A；CSV/XLSX/JSON 上传 → 行级校验 → 落库）。"""

    __tablename__ = "kb_test_sets"
    __table_args__ = (
        CheckConstraint(
            f"file_format IN ({_TEST_SET_FORMAT_SQL})", name="file_format_valid"
        ),
        CheckConstraint("case_count >= 0", name="case_count_nonnegative"),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_test_sets_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_test_sets_document_version_id_kb_document_versions",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_test_sets_created_by_users",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_kb_test_sets_id_tenant_id"),
        Index("ix_kb_test_sets_tenant_id", "tenant_id"),
        Index("ix_kb_test_sets_document_version_id", "document_version_id"),
    )

    document_version_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    file_object_key: Mapped[str] = mapped_column(String(300), nullable=False)
    file_format: Mapped[str] = mapped_column(String(10), nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_by: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)


class KbTestCase(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """测试用例（一行一条；question 必填，其余可选）。"""

    __tablename__ = "kb_test_cases"
    __table_args__ = (
        CheckConstraint("row_no >= 1", name="row_no_positive"),
        ForeignKeyConstraint(
            ["test_set_id", "tenant_id"],
            ["kb_test_sets.id", "kb_test_sets.tenant_id"],
            name="fk_kb_test_cases_test_set_id_kb_test_sets",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_kb_test_cases_id_tenant_id"),
        UniqueConstraint("test_set_id", "row_no", name="uq_kb_test_cases_test_set_row_no"),
        Index("ix_kb_test_cases_tenant_id", "tenant_id"),
        Index("ix_kb_test_cases_test_set_id", "test_set_id"),
    )

    test_set_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    expected_answer: Mapped[str | None] = mapped_column(Text)
    expected_points: Mapped[list[str] | None] = mapped_column(JSONB)
    expected_chunk_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    category: Mapped[str | None] = mapped_column(String(64))
    is_adversarial: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class KbTestRun(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """测试运行（READY→TESTING→TEST_PASSED/FAILED；活跃唯一由部分唯一索引兜底）。"""

    __tablename__ = "kb_test_runs"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_TEST_RUN_STATUS_SQL})", name="status_valid"
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_test_runs_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["test_set_id", "tenant_id"],
            ["kb_test_sets.id", "kb_test_sets.tenant_id"],
            name="fk_kb_test_runs_test_set_id_kb_test_sets",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_test_runs_version_id_kb_document_versions",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["started_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_test_runs_started_by_users",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_kb_test_runs_id_tenant_id"),
        Index(
            "uq_kb_test_runs_tenant_version_testing",
            "tenant_id",
            "version_id",
            unique=True,
            postgresql_where=text("status = 'TESTING'"),
        ),
        Index("ix_kb_test_runs_tenant_id", "tenant_id"),
    )

    test_set_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    version_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="TESTING")
    metrics_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # KB-09B：批量运行判分视角人群（token 字符串数组；null=发起人自身视角）。
    # 结构校验在服务层（parse_audience_tokens）；不加 as_user_id/ignore_acl（宁严勿松）
    as_audiences: Mapped[list[Any] | None] = mapped_column(JSONB)
    report_object_key: Mapped[str | None] = mapped_column(String(300))
    started_by: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
