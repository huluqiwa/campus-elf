"""M3 KB-02：审批引擎四表（approval_*）+ 构建任务表（kb_build_jobs）+ 状态 CHECK 放宽。

`docs/design-briefs/M3-KB-02.md`（已确认：D2 build 端点进本点、D3 自批恒 403、
D6 最小引擎集、不建版本表反向指针）。

- 五表均为租户级：`tenant_id NOT NULL` + FORCE RLS + 租户角色策略 + GRANT；
- `approval_requests.document_version_id` 可空（仅 KB_DOCUMENT 类型），部分唯一索引
  `(tenant_id, document_version_id) WHERE status='PENDING'` 兜底「一版本一活跃单」；
- `kb_build_jobs` 部分唯一索引 `(tenant_id, version_id) WHERE status IN ('BUILD_QUEUED','BUILDING')`
  兜底「一版本一活跃构建」（BUILDING 随 KB-03 放宽，索引进来避免届时重建）；
- `kb_documents` / `kb_document_versions` 的 status CHECK 加入 `APPROVED` / `REJECTED`
  （约束名不变，drop 后按原名重建）。

downgrade：恢复原状态 CHECK，按 动作 → 步骤 → 审批单 → 类型、构建任务 顺序删表。
**数据前提**：`kb_document_versions`/`kb_documents` 不在删除范围，若库里已有
APPROVED/REJECTED 行，重建旧 CHECK 会因存量违约失败——回滚前需先把决定态行迁回
UPLOADED/PENDING_APPROVAL（或清库）；预生产/开发库按此处理。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "d5e9a3c7b1f4"
down_revision: str | None = "b3d7f9a1c5e2"
branch_labels: str | None = None
depends_on: str | None = None

_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)

_REQUEST_STATUS_SQL = "status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')"
_REQUEST_RESULT_SQL = "result IS NULL OR result IN ('approve', 'reject', 'cancel')"
_ACTION_SQL = "action IN ('submit', 'approve', 'reject', 'cancel')"
_STEP_STATUS_SQL = "status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')"
_BUILD_JOB_STATUS_SQL = "status IN ('BUILD_QUEUED')"

# KB-01A 的原始状态集合与本点放宽后的集合（同名 CHECK，drop 后按原名重建）。
_STATUS_BEFORE = "status IN ('UPLOADED', 'PENDING_APPROVAL', 'SCAN_FAILED')"
_STATUS_AFTER = (
    "status IN ('UPLOADED', 'PENDING_APPROVAL', 'SCAN_FAILED', 'APPROVED', 'REJECTED')"
)
_STATUS_TABLES = ("kb_documents", "kb_document_versions")

_APPROVAL_TABLES = (
    "approval_actions",
    "approval_steps",
    "approval_requests",
    "approval_types",
)
_NEW_TABLES = (*_APPROVAL_TABLES, "kb_build_jobs")


def upgrade() -> None:
    op.create_table(
        "approval_types",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_approval_types_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_types")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_approval_types_id_tenant_id")),
        sa.UniqueConstraint("tenant_id", "code", name="uq_approval_types_tenant_code"),
    )
    op.create_index("ix_approval_types_tenant_id", "approval_types", ["tenant_id"])

    op.create_table(
        "approval_requests",
        sa.Column("type_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("submitter_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="PENDING", nullable=False),
        sa.Column("current_step", sa.Integer(), server_default="1", nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("payload_json", pg.JSONB(), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(_REQUEST_STATUS_SQL, name=op.f("ck_approval_requests_status_valid")),
        sa.CheckConstraint(_REQUEST_RESULT_SQL, name=op.f("ck_approval_requests_result_valid")),
        sa.CheckConstraint(
            "("
            "(status = 'PENDING' AND result IS NULL AND completed_at IS NULL)"
            " OR (status <> 'PENDING' AND result IS NOT NULL AND completed_at IS NOT NULL)"
            ")",
            name=op.f("ck_approval_requests_result_completion_consistent"),
        ),
        sa.CheckConstraint(
            "current_step >= 1", name=op.f("ck_approval_requests_current_step_positive")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_approval_requests_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["type_id", "tenant_id"],
            ["approval_types.id", "approval_types.tenant_id"],
            name="fk_approval_requests_type_id_approval_types",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_approval_requests_document_version_id_kb_document_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["submitter_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_approval_requests_submitter_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_requests")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_approval_requests_id_tenant_id"),
    )
    # 部分唯一索引：同一版本最多一个 PENDING 审批单（与模型同表达式，防漂移）。
    op.create_index(
        "uq_approval_requests_tenant_version_pending",
        "approval_requests",
        ["tenant_id", "document_version_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index("ix_approval_requests_tenant_id", "approval_requests", ["tenant_id"])
    op.create_index(
        "ix_approval_requests_tenant_status_created",
        "approval_requests",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "ix_approval_requests_tenant_submitter",
        "approval_requests",
        ["tenant_id", "submitter_id"],
    )

    op.create_table(
        "approval_steps",
        sa.Column("request_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("step_no", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="PENDING", nullable=False),
        sa.Column("acted_by", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(_STEP_STATUS_SQL, name=op.f("ck_approval_steps_status_valid")),
        sa.CheckConstraint("step_no >= 1", name=op.f("ck_approval_steps_step_no_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_approval_steps_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["request_id", "tenant_id"],
            ["approval_requests.id", "approval_requests.tenant_id"],
            name="fk_approval_steps_request_id_approval_requests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["acted_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_approval_steps_acted_by_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_steps")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_approval_steps_id_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "request_id",
            "step_no",
            name="uq_approval_steps_tenant_request_step",
        ),
    )
    op.create_index("ix_approval_steps_tenant_id", "approval_steps", ["tenant_id"])

    op.create_table(
        "approval_actions",
        sa.Column("request_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(_ACTION_SQL, name=op.f("ck_approval_actions_action_valid")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_approval_actions_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["request_id", "tenant_id"],
            ["approval_requests.id", "approval_requests.tenant_id"],
            name="fk_approval_actions_request_id_approval_requests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_id", "tenant_id"],
            ["approval_steps.id", "approval_steps.tenant_id"],
            name="fk_approval_actions_step_id_approval_steps",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_approval_actions_actor_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_actions")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_approval_actions_id_tenant_id"),
    )
    op.create_index("ix_approval_actions_tenant_id", "approval_actions", ["tenant_id"])
    op.create_index(
        "ix_approval_actions_tenant_request",
        "approval_actions",
        ["tenant_id", "request_id", "created_at"],
    )
    op.create_index(
        "ix_approval_actions_tenant_actor",
        "approval_actions",
        ["tenant_id", "actor_id", "created_at"],
    )

    op.create_table(
        "kb_build_jobs",
        sa.Column("version_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="BUILD_QUEUED", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(_BUILD_JOB_STATUS_SQL, name=op.f("ck_kb_build_jobs_status_valid")),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_kb_build_jobs_attempt_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_build_jobs_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_build_jobs_version_id_kb_document_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_build_jobs_created_by_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_build_jobs")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_kb_build_jobs_id_tenant_id"),
    )
    # 部分唯一索引：同一版本最多一个活跃构建（BUILDING 随 KB-03 放宽，索引进来避免届时重建）。
    op.create_index(
        "uq_kb_build_jobs_tenant_version_active",
        "kb_build_jobs",
        ["tenant_id", "version_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('BUILD_QUEUED', 'BUILDING')"),
    )
    op.create_index("ix_kb_build_jobs_tenant_id", "kb_build_jobs", ["tenant_id"])
    op.create_index(
        "ix_kb_build_jobs_tenant_status_updated",
        "kb_build_jobs",
        ["tenant_id", "status", "updated_at"],
    )

    for table in _STATUS_TABLES:
        op.drop_constraint(op.f(f"ck_{table}_status_valid"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_status_valid"), table, _STATUS_AFTER)

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
        op.create_check_constraint(op.f(f"ck_{table}_status_valid"), table, _STATUS_BEFORE)
