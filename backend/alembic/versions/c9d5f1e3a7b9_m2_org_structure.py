"""M2-ORG01-03：组织结构五表（班级/课程/教师档案/学生档案/任教关系）+ class 范围行完整性。

`docs/design-briefs/M2-ORG01-03.md`（已确认：D1 授权行+同事务同步 / D2 班主任权威在 classes /
D3 身份证列位预留 / D4 档案与账号 1:1 同建）。

设计要点：

1. **五张租户级表**（design §4.2 形态）：`classes`/`courses`/`teachers`/`students`/
   `teacher_class_courses`。全部带 `tenant_id` + 复合外键（父子同租）+ RLS。
2. **D2 落地**：班主任/辅导员权威在 `classes`（head_teacher_id/counselor_id），
   `students` **不**落冗余列（与 design §4.2 原文偏离，已获用户确认，design.md 由本功能点回写）。
3. **D3 落地**：`teachers`/`students` 预留 `id_card_enc/id_card_hmac/id_card_mask`
   （nullable，本迁移后**无任何读写路径**；加密/掩码/接口在 ORG-04 交付）。
4. **D4 落地**：`teachers`/`students` 与 `users` 1:1（unique (tenant_id, user_id)，
   ondelete CASCADE——账号删除即档案删除）。
5. **class 范围行完整性（D1）**：`data_scopes` 加部分唯一索引
   `(user_id, scope_id) WHERE scope_type='class' AND user_id IS NOT NULL`，
   让「同一用户对同一班级的范围行」在数据库层不可重复；同步函数（app/org/service.py）
   负责增删，本索引是最后防线。
6. `students.class_id` 可空（ORG-05 验收口径：学生可以未分班，其人群集合仅含自身/角色）。

downgrade：直接删五张表与部分唯一索引（M2 首点，无历史数据语义要保留）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "c9d5f1e3a7b9"
down_revision: str | None = "a8c3e9f1b5d7"
branch_labels: str | None = None
depends_on: str | None = None

# 与 M1-A 基线迁移同一策略表达式：未设置/空串/非法 UUID 退化 NULL → fail-closed。
_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)

_ORG_TABLES = ("classes", "courses", "teachers", "students", "teacher_class_courses")


def _create_org_table(
    name: str,
    columns: list[sa.Column],
    *,
    uniques: tuple[sa.Constraint, ...] = (),
    checks: tuple[sa.Constraint, ...] = (),
    fks: tuple[sa.Constraint, ...] = (),
) -> None:
    """按基线迁移的列序约定建表：业务列在前，id/created_at/updated_at 在后。"""

    op.create_table(
        name,
        *columns,
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        # 与 TenantScopedMixin 声明逐字一致（ondelete="RESTRICT" 必须显式写出，
        # 否则 alembic check 视为与元数据漂移）；其余复合外键各自声明 ondelete。
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{name}_tenant_id_tenants", ondelete="RESTRICT"
        ),
        *fks,
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{name}")),
        *uniques,
        *checks,
    )


def upgrade() -> None:
    # ---- classes：班主任/辅导员权威源（D2） ----
    _create_org_table(
        "classes",
        [
            sa.Column("grade", sa.String(length=16), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("head_teacher_id", pg.UUID(as_uuid=True), nullable=True),
            sa.Column("counselor_id", pg.UUID(as_uuid=True), nullable=True),
        ],
        uniques=(
            sa.UniqueConstraint(
                "tenant_id", "grade", "name", name=op.f("uq_classes_tenant_id_grade_name")
            ),
            sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_classes_id_tenant_id")),
        ),
        fks=(
            sa.ForeignKeyConstraint(
                ["head_teacher_id", "tenant_id"],
                ["users.id", "users.tenant_id"],
                name=op.f("fk_classes_head_teacher_id_users"),
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["counselor_id", "tenant_id"],
                ["users.id", "users.tenant_id"],
                name=op.f("fk_classes_counselor_id_users"),
                ondelete="SET NULL",
            ),
        ),
    )

    # ---- courses ----
    _create_org_table(
        "courses",
        [
            sa.Column("code", sa.String(length=32), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
        ],
        uniques=(
            sa.UniqueConstraint("tenant_id", "code", name=op.f("uq_courses_tenant_id_code")),
            sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_courses_id_tenant_id")),
        ),
    )

    # ---- teachers：身份证列位预留（D3），本迁移后无读写路径 ----
    _create_org_table(
        "teachers",
        [
            sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
            sa.Column("employee_no", sa.String(length=32), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("age", sa.SmallInteger(), nullable=True),
            sa.Column("gender", sa.String(length=8), nullable=True),
            sa.Column("position", sa.String(length=64), nullable=True),
            sa.Column("id_card_enc", sa.Text(), nullable=True),
            sa.Column("id_card_hmac", sa.String(length=64), nullable=True),
            sa.Column("id_card_mask", sa.String(length=32), nullable=True),
        ],
        uniques=(
            sa.UniqueConstraint("tenant_id", "user_id", name=op.f("uq_teachers_tenant_id_user_id")),
            sa.UniqueConstraint(
                "tenant_id", "employee_no", name=op.f("uq_teachers_tenant_id_employee_no")
            ),
            sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_teachers_id_tenant_id")),
        ),
        checks=(
            sa.CheckConstraint(
                "age IS NULL OR age BETWEEN 16 AND 80", name=op.f("ck_teachers_age_range")
            ),
            sa.CheckConstraint(
                "gender IS NULL OR gender IN ('male', 'female', 'other')",
                name=op.f("ck_teachers_gender_valid"),
            ),
        ),
        fks=(
            sa.ForeignKeyConstraint(
                ["user_id", "tenant_id"],
                ["users.id", "users.tenant_id"],
                name=op.f("fk_teachers_user_id_users"),
                ondelete="CASCADE",
            ),
        ),
    )

    # ---- students：class_id 可空（未分班合法）；无班主任/辅导员冗余列（D2） ----
    _create_org_table(
        "students",
        [
            sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
            sa.Column("student_no", sa.String(length=32), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("gender", sa.String(length=8), nullable=True),
            sa.Column("class_id", pg.UUID(as_uuid=True), nullable=True),
            sa.Column("id_card_enc", sa.Text(), nullable=True),
            sa.Column("id_card_hmac", sa.String(length=64), nullable=True),
            sa.Column("id_card_mask", sa.String(length=32), nullable=True),
        ],
        uniques=(
            sa.UniqueConstraint("tenant_id", "user_id", name=op.f("uq_students_tenant_id_user_id")),
            sa.UniqueConstraint(
                "tenant_id", "student_no", name=op.f("uq_students_tenant_id_student_no")
            ),
        ),
        checks=(
            sa.CheckConstraint(
                "gender IS NULL OR gender IN ('male', 'female', 'other')",
                name=op.f("ck_students_gender_valid"),
            ),
        ),
        fks=(
            sa.ForeignKeyConstraint(
                ["user_id", "tenant_id"],
                ["users.id", "users.tenant_id"],
                name=op.f("fk_students_user_id_users"),
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["class_id", "tenant_id"],
                ["classes.id", "classes.tenant_id"],
                name=op.f("fk_students_class_id_classes"),
                ondelete="SET NULL",
            ),
        ),
    )

    # ---- teacher_class_courses：任教关系（class 范围行的事实来源之一） ----
    _create_org_table(
        "teacher_class_courses",
        [
            sa.Column("teacher_id", pg.UUID(as_uuid=True), nullable=False),
            sa.Column("class_id", pg.UUID(as_uuid=True), nullable=False),
            sa.Column("course_id", pg.UUID(as_uuid=True), nullable=False),
            sa.Column("term", sa.String(length=16), nullable=False),
        ],
        uniques=(
            sa.UniqueConstraint(
                "teacher_id", "class_id", "course_id", "term",
                name=op.f("uq_teacher_class_courses_teacher_id_class_id_course_id_term"),
            ),
        ),
        fks=(
            sa.ForeignKeyConstraint(
                ["teacher_id", "tenant_id"],
                ["teachers.id", "teachers.tenant_id"],
                name=op.f("fk_teacher_class_courses_teacher_id_teachers"),
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["class_id", "tenant_id"],
                ["classes.id", "classes.tenant_id"],
                name=op.f("fk_teacher_class_courses_class_id_classes"),
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["course_id", "tenant_id"],
                ["courses.id", "courses.tenant_id"],
                name=op.f("fk_teacher_class_courses_course_id_courses"),
                ondelete="CASCADE",
            ),
        ),
    )

    # ---- mixin 自带的 tenant_id 索引（与 TenantScopedMixin 声明一致，防 alembic 漂移） ----
    for table in _ORG_TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])

    # ---- RLS：与基线迁移同款（ENABLE + FORCE + 按数据库角色限定的租户策略） ----
    for table in _ORG_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} TO campus_elf_tenant "
            f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
        )

    # ---- 授权：平台角色不授（跨租读取走显式租户连接通道，M1-E2 口径 ②） ----
    for table in _ORG_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO campus_elf_tenant")

    # ---- class 范围行完整性（D1 最后防线） ----
    op.create_index(
        "uq_data_scopes_user_class",
        "data_scopes",
        ["user_id", "scope_id"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'class' AND user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_data_scopes_user_class", table_name="data_scopes")
    for table in reversed(_ORG_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
