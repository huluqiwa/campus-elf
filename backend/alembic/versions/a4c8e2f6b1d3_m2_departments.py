"""M2-ORG05：部门/科室树（departments）+ 教师部门归属（teachers.department_id）。

`docs/design-briefs/M2-ORG05-departments.md`（已确认：D1 邻接表+id 物化路径 /
D2 教师单一部门 / D3 导入名称全路径 / D4 叶子+无归属才可硬删）。

设计要点：
- `path` 为 id 物化路径（`/{root_id}/{child_id}/…`，**以自身 id 结尾**）：
  子树查询 `path LIKE '{自身 path}/%'`（M3 父部门人群命中子部门就绪）；改名不动 path；
- 根部门（parent_id IS NULL）的同级唯一性由**部分唯一索引**保证
  （Postgres 唯一约束视 NULL 为互异，(tenant_id, parent_id, name) 约束覆盖不到根）；
- `teachers.department_id` 可空（调岗前可不归属），ondelete SET NULL。

downgrade：删表 + 摘列（M2 中段，无历史数据语义要保留）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "a4c8e2f6b1d3"
down_revision: str | None = "e2a6c8d4b0f2"
branch_labels: str | None = None
depends_on: str | None = None

_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)


def upgrade() -> None:
    op.create_table(
        "departments",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=True),
        sa.Column("parent_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("path", sa.String(length=600), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name=op.f("ck_departments_status_valid")
        ),
        sa.CheckConstraint("parent_id <> id", name=op.f("ck_departments_parent_not_self")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_departments_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id", "tenant_id"],
            ["departments.id", "departments.tenant_id"],
            name=op.f("fk_departments_parent_id_departments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_departments")),
        sa.UniqueConstraint("tenant_id", "parent_id", "name",
                            name=op.f("uq_departments_tenant_id_parent_id_name")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_departments_id_tenant_id")),
    )
    op.create_index("ix_departments_tenant_id", "departments", ["tenant_id"])
    # 根部门（parent_id IS NULL）的同级唯一性：唯一约束视 NULL 为互异，覆盖不到
    op.create_index(
        "uq_departments_tenant_root_name",
        "departments",
        ["tenant_id", "name"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NULL"),
    )
    # 子树前缀查询（M3 父部门人群命中子部门）：path 天然非唯一，普通索引
    op.create_index(
        "ix_departments_path", "departments", ["path"],
        postgresql_ops={"path": "varchar_pattern_ops"},
    )

    op.execute("ALTER TABLE departments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE departments FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON departments TO campus_elf_tenant "
        f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON departments TO campus_elf_tenant")

    # teachers.department_id：可空，部门删除 → 置 NULL（教师档案保留）
    op.add_column(
        "teachers",
        sa.Column("department_id", pg.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_teachers_tenant_id_department_id", "teachers",
        ["tenant_id", "department_id"],
    )
    # 复合外键用原生 SQL：op.create_foreign_key 会对同批次新建的 departments 表
    # 触发 alembic 侧元数据解析（NoReferencedTableError，M2-ORG05 实测）
    op.execute(
        "ALTER TABLE teachers ADD CONSTRAINT fk_teachers_department_id_departments "
        "FOREIGN KEY (department_id, tenant_id) "
        "REFERENCES departments (id, tenant_id) ON DELETE SET NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE teachers DROP CONSTRAINT IF EXISTS fk_teachers_department_id_departments")
    op.drop_index("ix_teachers_tenant_id_department_id", table_name="teachers")
    op.drop_column("teachers", "department_id")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON departments")
    op.drop_index("ix_departments_path", table_name="departments")
    op.drop_index("uq_departments_tenant_root_name", table_name="departments")
    op.drop_index("ix_departments_tenant_id", table_name="departments")
    op.drop_table("departments")
