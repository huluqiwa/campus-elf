"""M2-ORG02：导入任务两表（import_jobs / import_row_errors）。

`docs/design-briefs/M2-ORG02-import.md`（已确认：D1 Arq / D2 随机口令+重置分发 /
D3 conflict=error|skip / D4 openpyxl+对象存储）。

对 design §4.2 原文的偏离（已记录，design.md 随本点回写）：
- `import_jobs` 增 `created_by`（上传者，审计/归属）、`row_filter_json`（重试 job 只导失败行）、
  `retried_by`（源 job 指向派生 job）、`started_at/finished_at/message/conflict`；
- `import_row_errors` 增 `raw_json`（原始行数据，重试免二次解析整文件）。

downgrade：直接删两表（M2 首点，无历史数据语义要保留）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "e2a6c8d4b0f2"
down_revision: str | None = "c9d5f1e3a7b9"
branch_labels: str | None = None
depends_on: str | None = None

_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)

_IMPORT_TABLES = ("import_jobs", "import_row_errors")


def upgrade() -> None:
    op.create_table(
        "import_jobs",
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("conflict", sa.String(length=8), nullable=False, server_default="error"),
        sa.Column("file_object_key", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("total_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_report_key", sa.String(length=300), nullable=True),
        sa.Column("row_filter_json", pg.JSONB(), nullable=True),
        sa.Column("retried_by", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "type IN ('teachers', 'students')", name=op.f("ck_import_jobs_type_valid")
        ),
        sa.CheckConstraint(
            "conflict IN ('error', 'skip')", name=op.f("ck_import_jobs_conflict_valid")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'completed_with_errors', 'failed')",
            name=op.f("ck_import_jobs_status_valid"),
        ),
        sa.CheckConstraint(
            "total_rows >= 0 AND success_rows >= 0 AND failed_rows >= 0 AND skipped_rows >= 0",
            name=op.f("ck_import_jobs_counters_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_import_jobs_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name=op.f("fk_import_jobs_created_by_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_jobs")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_import_jobs_id_tenant_id")),
    )
    op.create_index("ix_import_jobs_tenant_id", "import_jobs", ["tenant_id"])

    op.create_table(
        "import_row_errors",
        sa.Column("job_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("error_code", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("raw_json", pg.JSONB(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_import_row_errors_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "tenant_id"],
            ["import_jobs.id", "import_jobs.tenant_id"],
            name=op.f("fk_import_row_errors_job_id_import_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_row_errors")),
    )
    op.create_index("ix_import_row_errors_tenant_id", "import_row_errors", ["tenant_id"])
    op.create_index("ix_import_row_errors_job_id", "import_row_errors", ["job_id"])

    for table in _IMPORT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} TO campus_elf_tenant "
            f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO campus_elf_tenant")


def downgrade() -> None:
    for table in reversed(_IMPORT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
