"""M3 KB-01A：知识库上传三表（kb_upload_batches / kb_documents / kb_document_versions）。

`docs/design-briefs/M3-KB-01-upload.md`（已确认：D3 整批原子 + 必填幂等键、
D4 对象 key 带租户、D5 不新增 SCANNING 状态）。

- 三表均为租户级：`tenant_id NOT NULL` + FORCE RLS + 租户角色策略 + GRANT；
- `kb_documents.current_version_id` 与版本表循环引用，建表后再补复合外键；
- 本点状态 CHECK 只允许 `UPLOADED / PENDING_APPROVAL / SCAN_FAILED`，
  后续 KB-02/03 扩展状态机时随迁移放宽。

downgrade：先删循环外键，再按版本 → 文档 → 批次删表。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "b3d7f9a1c5e2"
down_revision: str | None = "a1c3e5f7b9d2"
branch_labels: str | None = None
depends_on: str | None = None

_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)

_KB_TABLES = ("kb_document_versions", "kb_documents", "kb_upload_batches")
_STATUS_SQL = "status IN ('UPLOADED', 'PENDING_APPROVAL', 'SCAN_FAILED')"


def upgrade() -> None:
    op.create_table(
        "kb_upload_batches",
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_by", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "file_count > 0 AND file_count <= 15",
            name=op.f("ck_kb_upload_batches_file_count_range"),
        ),
        sa.CheckConstraint(
            "total_bytes > 0", name=op.f("ck_kb_upload_batches_total_bytes_positive")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_upload_batches_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_upload_batches_created_by_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_upload_batches")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_kb_upload_batches_id_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_kb_upload_batches_tenant_idempotency_key",
        ),
    )
    op.create_index("ix_kb_upload_batches_tenant_id", "kb_upload_batches", ["tenant_id"])

    op.create_table(
        "kb_documents",
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("tags", pg.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="UPLOADED", nullable=False),
        sa.Column("current_version_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("upload_batch_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("upload_file_index", sa.Integer(), nullable=False),
        sa.Column("created_by", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(_STATUS_SQL, name=op.f("ck_kb_documents_status_valid")),
        sa.CheckConstraint(
            "upload_file_index >= 0",
            name=op.f("ck_kb_documents_upload_file_index_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_documents_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["upload_batch_id", "tenant_id"],
            ["kb_upload_batches.id", "kb_upload_batches.tenant_id"],
            name="fk_kb_documents_upload_batch_id_kb_upload_batches",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_documents_created_by_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_documents")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_kb_documents_id_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "upload_batch_id",
            "upload_file_index",
            name="uq_kb_documents_tenant_upload_batch_index",
        ),
    )
    op.create_index("ix_kb_documents_tenant_id", "kb_documents", ["tenant_id"])
    op.create_index(
        "ix_kb_documents_tenant_created_at_id", "kb_documents", ["tenant_id", "created_at", "id"]
    )

    op.create_table(
        "kb_document_versions",
        sa.Column("document_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("source_object_key", sa.String(length=300), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("mime", sa.String(length=127), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="UPLOADED", nullable=False),
        sa.Column("scan_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("scan_error_code", sa.String(length=32), nullable=True),
        sa.Column("scan_signature", sa.String(length=200), nullable=True),
        sa.Column("scan_engine", sa.String(length=32), nullable=True),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(_STATUS_SQL, name=op.f("ck_kb_document_versions_status_valid")),
        sa.CheckConstraint(
            "version_no >= 1", name=op.f("ck_kb_document_versions_version_no_positive")
        ),
        sa.CheckConstraint(
            "size_bytes > 0", name=op.f("ck_kb_document_versions_size_bytes_positive")
        ),
        sa.CheckConstraint(
            "scan_attempts >= 0",
            name=op.f("ck_kb_document_versions_scan_attempts_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_document_versions_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["kb_documents.id", "kb_documents.tenant_id"],
            name="fk_kb_document_versions_document_id_kb_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_document_versions_created_by_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_document_versions")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_kb_document_versions_id_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "document_id",
            "version_no",
            name="uq_kb_document_versions_tenant_document_version",
        ),
    )
    op.create_index("ix_kb_document_versions_tenant_id", "kb_document_versions", ["tenant_id"])
    op.create_index(
        "ix_kb_document_versions_document_id", "kb_document_versions", ["document_id"]
    )
    op.create_index(
        "ix_kb_document_versions_tenant_status_updated",
        "kb_document_versions",
        ["tenant_id", "status", "updated_at"],
    )

    op.create_foreign_key(
        "fk_kb_documents_current_version_id_kb_document_versions",
        "kb_documents",
        "kb_document_versions",
        ["current_version_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )

    for table in _KB_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} TO campus_elf_tenant "
            f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO campus_elf_tenant")


def downgrade() -> None:
    op.drop_constraint(
        "fk_kb_documents_current_version_id_kb_document_versions",
        "kb_documents",
        type_="foreignkey",
    )
    for table in _KB_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
