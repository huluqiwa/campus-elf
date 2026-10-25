"""M2-ORG06：入学年份与学年流转（classes 模型修订 + 流转批次表）。

`docs/design-briefs/M2-ORG06-academic-year.md`（已确认：唯一键=入学年份+班名 /
年级=当前年级可流转 / 归档=行保留+审计 / 学生账号本点不动）：

- `classes.entry_year`：入学年份（届身份，建档后不可改）；存量测试数据回填 2024
  （测试库无生产语义，流转前由校方建班时给真实值）；
- `classes.grade`：语义改为「当前年级」，列名不变（避免无谓的重命名迁移面）；
- 唯一键 `(tenant_id, grade, name)` → `(tenant_id, entry_year, name)`：
  同届班名唯一、跨届同名共存；(当前年级, 班名) 不设唯一（简报 D1：先建新届再
  流转的顺序不得撞名）；
- `classes.status`：active/archived，归档行永不删除；
- `academic_year_rollovers`：流转批次表，`(tenant_id, idempotency_key)` 唯一
  ——重复提交 409 的数据库防线（防把新届班误升）。

downgrade：摘列/换回唯一键/删批次表（M2 中段，无历史数据语义要保留）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "b9d3f7c1e5a9"
down_revision: str | None = "f8b2d6a0c4e8"
branch_labels: str | None = None
depends_on: str | None = None

_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)


def upgrade() -> None:
    # ---- classes：入学年份 + 归档状态 + 唯一键切换 ----
    op.add_column(
        "classes",
        sa.Column("entry_year", sa.Integer(), nullable=False, server_default="2024"),
    )
    op.alter_column("classes", "entry_year", server_default=None)
    op.add_column(
        "classes",
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
    )
    op.alter_column("classes", "status", server_default=None)
    op.create_check_constraint(
        "ck_classes_status_valid", "classes", "status IN ('active', 'archived')"
    )
    op.create_check_constraint(
        "ck_classes_entry_year_range", "classes", "entry_year BETWEEN 1970 AND 2100"
    )
    op.drop_constraint("uq_classes_tenant_id_grade_name", "classes", type_="unique")
    op.create_unique_constraint(
        "uq_classes_tenant_entry_year_name", "classes", ["tenant_id", "entry_year", "name"]
    )

    # ---- academic_year_rollovers：流转批次（幂等键唯一 = 防重复执行） ----
    op.create_table(
        "academic_year_rollovers",
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("mapping_json", pg.JSONB(), nullable=False),
        sa.Column("archive_grades_json", pg.JSONB(), nullable=False),
        sa.Column("advanced_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archived_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("operator_user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "advanced_count >= 0 AND archived_count >= 0",
            name=op.f("ck_academic_year_rollovers_counters_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_academic_year_rollovers_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operator_user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name=op.f("fk_academic_year_rollovers_operator_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_academic_year_rollovers")),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name=op.f("uq_academic_year_rollovers_tenant_id_idempotency_key"),
        ),
    )
    op.create_index(
        "ix_academic_year_rollovers_tenant_id", "academic_year_rollovers", ["tenant_id"]
    )

    op.execute("ALTER TABLE academic_year_rollovers ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE academic_year_rollovers FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON academic_year_rollovers TO campus_elf_tenant "
        f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON academic_year_rollovers TO campus_elf_tenant"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON academic_year_rollovers")
    op.drop_table("academic_year_rollovers")
    op.drop_constraint("uq_classes_tenant_entry_year_name", "classes", type_="unique")
    op.create_unique_constraint(
        "uq_classes_tenant_id_grade_name", "classes", ["tenant_id", "grade", "name"]
    )
    op.drop_constraint("ck_classes_entry_year_range", "classes", type_="check")
    op.drop_constraint("ck_classes_status_valid", "classes", type_="check")
    op.drop_column("classes", "status")
    op.drop_column("classes", "entry_year")
