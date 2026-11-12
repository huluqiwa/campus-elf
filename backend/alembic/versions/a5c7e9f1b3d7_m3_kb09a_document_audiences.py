"""M3 KB-09A：文档查阅人群 ACL——kb_document_audiences 表 + 确认态列。

`docs/design-briefs/M3-KB-09.md`（已确认 D1-A 门禁形态 / D2-B payload 单一事实源 /
卡 5 工程事实）。fail-closed 门禁（spec KB-09 :307）：0 条 → `ACL_REQUIRED`，
已配置未确认 → `ACL_UNCONFIRMED`（门禁代码随本迁移同批的模型/服务实现交付）。

- 新表 `kb_document_audiences`：版本级查阅人群，`audience_type ∈ {department, role,
  class, grade, course, user, all}` 多条并集（spec :306）；`audience_id` VARCHAR(64)
  多态引用（无 FK——UUID 存字符串、`grade` 存年级字符串，`classes.grade` 为
  String(16)；存在性与同租校验在服务层）；`all` 行 audience_id 为空；唯一
  `(version_id, audience_type, audience_id)` 用 NULLS NOT DISTINCT（PG16——防止
  `all` 行 audience_id NULL 重复）；租户级 + FORCE RLS + 策略（kb_publish_aliases
  同款）；
- `kb_document_versions` 加 `acl_confirmed_by`/`acl_confirmed_at`（nullable；
  null=未确认——design §5 :221 口径）；PUT 人群 = 全量替换且置未确认（spec :309
  「保存后置为未确认」），confirm 置 by/at（`school.kb.acl.manage`）。

downgrade：删确认态列、删新表。
**数据前提**：无（新表空降、新列可空，downgrade 无数据迁移要求）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "a5c7e9f1b3d7"
down_revision: str | None = "c9e1f3a5d7b9"
branch_labels: str | None = None
depends_on: str | None = None

_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)

_AUDIENCE_TYPE_CHECK = (
    "audience_type IN ('department', 'role', 'class', 'grade', 'course', 'user', 'all')"
)


def upgrade() -> None:
    op.create_table(
        "kb_document_audiences",
        sa.Column("audience_type", sa.String(length=24), nullable=False),
        sa.Column("audience_id", sa.String(length=64), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("created_by", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", pg.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_document_audiences_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_document_audiences_version_id_kb_document_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_document_audiences_created_by_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_document_audiences")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_kb_document_audiences_id_tenant_id"),
        sa.UniqueConstraint(
            "version_id",
            "audience_type",
            "audience_id",
            name="uq_kb_document_audiences_version_type_audience",
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint(
            _AUDIENCE_TYPE_CHECK,
            name=op.f("ck_kb_document_audiences_audience_type_valid"),
        ),
    )
    op.create_index("ix_kb_document_audiences_tenant_id", "kb_document_audiences", ["tenant_id"])
    op.create_index(
        "ix_kb_document_audiences_version_id", "kb_document_audiences", ["version_id"]
    )

    op.execute("ALTER TABLE kb_document_audiences ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE kb_document_audiences FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON kb_document_audiences TO campus_elf_tenant "
        f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON kb_document_audiences TO campus_elf_tenant"
    )

    op.add_column(
        "kb_document_versions",
        sa.Column("acl_confirmed_by", pg.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "kb_document_versions",
        sa.Column(
            "acl_confirmed_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.create_foreign_key(
        "fk_kb_document_versions_acl_confirmed_by_users",
        "kb_document_versions",
        "users",
        ["acl_confirmed_by", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_kb_document_versions_acl_confirmed_by_users", "kb_document_versions", type_="foreignkey"
    )
    op.drop_column("kb_document_versions", "acl_confirmed_at")
    op.drop_column("kb_document_versions", "acl_confirmed_by")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON kb_document_audiences")
    op.drop_table("kb_document_audiences")
