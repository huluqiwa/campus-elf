"""M3 KB-04A：测试沙箱三表（kb_test_sets / kb_test_cases / kb_test_runs）+ 状态放宽。

`docs/design-briefs/M3-KB-04A.md`（按推荐项执行：D1 检索命中 MVP、D2 CSV/XLSX/JSON、
D3 TEST_PASSED 可重跑、D4 Qdrant 原生 RRF）。

- 三表均租户级 + FORCE RLS + 策略；
- `kb_test_cases` 绑 test_set（CASCADE），行号唯一；
- `kb_test_runs` 部分唯一索引 `(tenant_id, version_id) WHERE status='TESTING'`
  兜底「一版本一个活跃测试运行」；
- `kb_document_versions`/`kb_documents` status CHECK 加入 `TESTING/TEST_PASSED/TEST_FAILED`。

downgrade：删三表、恢复原状态 CHECK（数据前提：有 TESTING/TEST_* 行时需先迁移）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "b7d3e9f1a5c3"
down_revision: str | None = "f2a8b4c6e0d7"
branch_labels: str | None = None
depends_on: str | None = None

_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)

_VERSION_STATUS_BEFORE = (
    "status IN ('UPLOADED', 'PENDING_APPROVAL', 'SCAN_FAILED', 'APPROVED', 'REJECTED', "
    "'BUILDING', 'READY', 'FAILED')"
)
_VERSION_STATUS_AFTER = (
    "status IN ('UPLOADED', 'PENDING_APPROVAL', 'SCAN_FAILED', 'APPROVED', 'REJECTED', "
    "'BUILDING', 'READY', 'FAILED', 'TESTING', 'TEST_PASSED', 'TEST_FAILED')"
)
_STATUS_TABLES = ("kb_documents", "kb_document_versions")
_NEW_TABLES = ("kb_test_runs", "kb_test_cases", "kb_test_sets")


def upgrade() -> None:
    op.create_table(
        "kb_test_sets",
        sa.Column("document_version_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("file_object_key", sa.String(length=300), nullable=False),
        sa.Column("file_format", sa.String(length=10), nullable=False),
        sa.Column("case_count", sa.Integer(), server_default="0", nullable=False),
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
            "file_format IN ('csv', 'xlsx', 'json')", name=op.f("ck_kb_test_sets_file_format_valid")
        ),
        sa.CheckConstraint("case_count >= 0", name=op.f("ck_kb_test_sets_case_count_nonnegative")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_test_sets_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_test_sets_document_version_id_kb_document_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_test_sets_created_by_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_test_sets")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_kb_test_sets_id_tenant_id"),
    )
    op.create_index("ix_kb_test_sets_tenant_id", "kb_test_sets", ["tenant_id"])
    op.create_index(
        "ix_kb_test_sets_document_version_id", "kb_test_sets", ["document_version_id"]
    )

    op.create_table(
        "kb_test_cases",
        sa.Column("test_set_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("expected_answer", sa.Text(), nullable=True),
        sa.Column("expected_points", pg.JSONB(), nullable=True),
        sa.Column("expected_chunk_ids", pg.JSONB(), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("is_adversarial", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("row_no >= 1", name=op.f("ck_kb_test_cases_row_no_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_test_cases_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["test_set_id", "tenant_id"],
            ["kb_test_sets.id", "kb_test_sets.tenant_id"],
            name="fk_kb_test_cases_test_set_id_kb_test_sets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_test_cases")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_kb_test_cases_id_tenant_id"),
        sa.UniqueConstraint(
            "test_set_id", "row_no", name="uq_kb_test_cases_test_set_row_no"
        ),
    )
    op.create_index("ix_kb_test_cases_tenant_id", "kb_test_cases", ["tenant_id"])
    op.create_index("ix_kb_test_cases_test_set_id", "kb_test_cases", ["test_set_id"])

    op.create_table(
        "kb_test_runs",
        sa.Column("test_set_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="TESTING", nullable=False),
        sa.Column("metrics_json", pg.JSONB(), nullable=True),
        sa.Column("report_object_key", sa.String(length=300), nullable=True),
        sa.Column("started_by", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('TESTING', 'TEST_PASSED', 'TEST_FAILED', 'STALE')",
            name=op.f("ck_kb_test_runs_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_test_runs_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["test_set_id", "tenant_id"],
            ["kb_test_sets.id", "kb_test_sets.tenant_id"],
            name="fk_kb_test_runs_test_set_id_kb_test_sets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_test_runs_version_id_kb_document_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["started_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_test_runs_started_by_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_test_runs")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_kb_test_runs_id_tenant_id"),
    )
    op.create_index("ix_kb_test_runs_tenant_id", "kb_test_runs", ["tenant_id"])
    op.create_index(
        "uq_kb_test_runs_tenant_version_testing",
        "kb_test_runs",
        ["tenant_id", "version_id"],
        unique=True,
        postgresql_where=sa.text("status = 'TESTING'"),
    )

    for table in _NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} TO campus_elf_tenant "
            f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO campus_elf_tenant")

    for table in _STATUS_TABLES:
        op.drop_constraint(op.f(f"ck_{table}_status_valid"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_status_valid"), table, _VERSION_STATUS_AFTER)


def downgrade() -> None:
    for table in _NEW_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
    for table in _STATUS_TABLES:
        op.drop_constraint(op.f(f"ck_{table}_status_valid"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_status_valid"), table, _VERSION_STATUS_BEFORE)
