"""M3 KB-03A：构建执行管线——kb_build_jobs 全字段 + kb_chunks + kb_index_versions。

`docs/design-briefs/M3-KB-03A.md`（已确认：D1 A/B 切分、D2 PDF/MD/TXT 先行、
D3 切块参数可配、D4 fake ports 进 CI）。

- `kb_build_jobs` 补 design §4.3 规划的全字段（stage/progress/units/tier/
  fallback_reason/retry_count/error_code/message/started_at/heartbeat_at/finished_at）
  并放宽 status CHECK（BUILDING/READY/FAILED）；部分唯一索引 KB-02 建时已预留
  BUILDING，无需重建；
- `kb_document_versions` / `kb_documents` 的 status CHECK 加入 `BUILDING/READY/FAILED`
  （约束名不变，drop 后按原名重建）；
- 新表 `kb_chunks`（父子块；`is_human_edited` 等 K5 列一并落为默认值，避免 KB-10 再迁移）
  与 `kb_index_versions`（版本化索引记录；alias 是 KB-05）：均租户级 + FORCE RLS + 策略；
  `kb_chunks.parent_id` 自引用复合外键（use_alter，建表后补）。

downgrade：删两新表、删 job 新增列、恢复原状态 CHECK。
**数据前提**：库里已有 BUILDING/READY/FAILED 版本/任务行时，恢复旧 CHECK 会违约——
回滚前需先把构建态数据迁回（或清库）；开发/预生产按此处理。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "e8c2a6d0f4b9"
down_revision: str | None = "d5e9a3c7b1f4"
branch_labels: str | None = None
depends_on: str | None = None

_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)

_VERSION_STATUS_BEFORE = (
    "status IN ('UPLOADED', 'PENDING_APPROVAL', 'SCAN_FAILED', 'APPROVED', 'REJECTED')"
)
_VERSION_STATUS_AFTER = (
    "status IN ('UPLOADED', 'PENDING_APPROVAL', 'SCAN_FAILED', 'APPROVED', 'REJECTED', "
    "'BUILDING', 'READY', 'FAILED')"
)
_JOB_STATUS_BEFORE = "status IN ('BUILD_QUEUED')"
_JOB_STATUS_AFTER = "status IN ('BUILD_QUEUED', 'BUILDING', 'READY', 'FAILED')"
_STAGE_SQL = "stage IS NULL OR stage IN ('PARSING', 'NORMALIZING', 'CHUNKING', 'EMBEDDING', 'INDEXING', 'VALIDATING')"

_STATUS_TABLES = ("kb_documents", "kb_document_versions")
_NEW_TABLES = ("kb_chunks", "kb_index_versions")


def upgrade() -> None:
    # 1) kb_build_jobs 全字段 + 状态放宽
    op.add_column("kb_build_jobs", sa.Column("stage", sa.String(length=24), nullable=True))
    op.add_column("kb_build_jobs", sa.Column("progress", sa.Float(), nullable=True))
    op.add_column("kb_build_jobs", sa.Column("processed_units", sa.Integer(), nullable=True))
    op.add_column("kb_build_jobs", sa.Column("total_units", sa.Integer(), nullable=True))
    op.add_column("kb_build_jobs", sa.Column("tier", sa.String(length=32), nullable=True))
    op.add_column("kb_build_jobs", sa.Column("fallback_reason", sa.Text(), nullable=True))
    op.add_column(
        "kb_build_jobs",
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("kb_build_jobs", sa.Column("error_code", sa.String(length=64), nullable=True))
    op.add_column("kb_build_jobs", sa.Column("message", sa.Text(), nullable=True))
    op.add_column(
        "kb_build_jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "kb_build_jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "kb_build_jobs", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(op.f("ck_kb_build_jobs_stage_valid"), "kb_build_jobs", _STAGE_SQL)
    op.create_check_constraint(
        op.f("ck_kb_build_jobs_progress_range"),
        "kb_build_jobs",
        "progress IS NULL OR (progress >= 0 AND progress <= 1)",
    )
    op.create_check_constraint(
        op.f("ck_kb_build_jobs_retry_count_nonnegative"),
        "kb_build_jobs",
        "retry_count >= 0",
    )
    op.drop_constraint(op.f("ck_kb_build_jobs_status_valid"), "kb_build_jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_kb_build_jobs_status_valid"), "kb_build_jobs", _JOB_STATUS_AFTER
    )
    op.create_index(
        "ix_kb_build_jobs_tenant_heartbeat",
        "kb_build_jobs",
        ["tenant_id", "status", "heartbeat_at"],
    )

    # 2) 版本/文档状态 CHECK 放宽（同名重建）
    for table in _STATUS_TABLES:
        op.drop_constraint(op.f(f"ck_{table}_status_valid"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_status_valid"), table, _VERSION_STATUS_AFTER)

    # 3) kb_chunks
    op.create_table(
        "kb_chunks",
        sa.Column("document_version_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("chunk_type", sa.String(length=16), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_markdown", sa.Text(), nullable=False),
        sa.Column("section_path", sa.String(length=500), nullable=True),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("char_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("overlap_prev", sa.Integer(), nullable=True),
        sa.Column("overlap_next", sa.Integer(), nullable=True),
        sa.Column("chunk_hash", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", pg.JSONB(), nullable=True),
        sa.Column(
            "embedding_status", sa.String(length=16), server_default="pending", nullable=False
        ),
        sa.Column("is_human_edited", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("edited_by", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "chunk_type IN ('parent', 'child')", name=op.f("ck_kb_chunks_chunk_type_valid")
        ),
        sa.CheckConstraint(
            "embedding_status IN ('pending', 'ready', 'failed')",
            name=op.f("ck_kb_chunks_embedding_status_valid"),
        ),
        sa.CheckConstraint("chunk_index >= 0", name=op.f("ck_kb_chunks_chunk_index_nonnegative")),
        sa.CheckConstraint(
            "token_count >= 0 AND char_count >= 0", name=op.f("ck_kb_chunks_count_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_chunks_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_chunks_document_version_id_kb_document_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["edited_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_chunks_edited_by_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_chunks")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_kb_chunks_id_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "document_version_id",
            "chunk_index",
            name="uq_kb_chunks_tenant_version_chunk_index",
        ),
    )
    op.create_index("ix_kb_chunks_tenant_id", "kb_chunks", ["tenant_id"])
    op.create_index("ix_kb_chunks_document_version_id", "kb_chunks", ["document_version_id"])
    # 自引用复合外键（父块），建表后补
    op.create_foreign_key(
        "fk_kb_chunks_parent_id_kb_chunks",
        "kb_chunks",
        "kb_chunks",
        ["parent_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="CASCADE",
    )

    # 4) kb_index_versions
    op.create_table(
        "kb_index_versions",
        sa.Column("document_version_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("qdrant_collection", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="building", nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('building', 'ready', 'published', 'retired')",
            name=op.f("ck_kb_index_versions_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_index_versions_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_index_versions_document_version_id_kb_document_versions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_index_versions")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_kb_index_versions_id_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "document_version_id",
            name="uq_kb_index_versions_tenant_version",
        ),
    )
    op.create_index("ix_kb_index_versions_tenant_id", "kb_index_versions", ["tenant_id"])

    for table in _NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} TO campus_elf_tenant "
            f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO campus_elf_tenant")


def downgrade() -> None:
    for table in _NEW_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
    for table in _STATUS_TABLES:
        op.drop_constraint(op.f(f"ck_{table}_status_valid"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_status_valid"), table, _VERSION_STATUS_BEFORE)
    op.drop_index("ix_kb_build_jobs_tenant_heartbeat", table_name="kb_build_jobs")
    op.drop_constraint(op.f("ck_kb_build_jobs_status_valid"), "kb_build_jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_kb_build_jobs_status_valid"), "kb_build_jobs", _JOB_STATUS_BEFORE
    )
    op.drop_constraint(op.f("ck_kb_build_jobs_stage_valid"), "kb_build_jobs", type_="check")
    op.drop_constraint(op.f("ck_kb_build_jobs_progress_range"), "kb_build_jobs", type_="check")
    op.drop_constraint(
        op.f("ck_kb_build_jobs_retry_count_nonnegative"), "kb_build_jobs", type_="check"
    )
    for column in (
        "finished_at",
        "heartbeat_at",
        "started_at",
        "message",
        "error_code",
        "retry_count",
        "fallback_reason",
        "tier",
        "total_units",
        "processed_units",
        "progress",
        "stage",
    ):
        op.drop_column("kb_build_jobs", column)
