"""组织结构模型（M2 ORG-01/03）：班级、课程、教师档案、学生档案、任教关系。

口径（`docs/design-briefs/M2-ORG01-03.md`，已确认）：

- **D2**：班主任/辅导员权威在 `classes`；`students` 不落冗余列（响应里由服务层 join 聚合）；
- **D3**：`id_card_*` 三列自 ORG-04 起由 `app/org/id_card_service.py` 读写
  （enc/hmac/mask 三件套；明文不落日志/审计）；
- **D4**：档案与账号 1:1（unique (tenant_id, user_id)，账号删除级联删档案）；
- **D1**：class 范围行的数据库完整性由迁移里的部分唯一索引保证
  （`uq_data_scopes_user_class`，见 `rbac.DataScope`），本模块的同步函数负责增删。
"""

from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

# 与 users.user_type 同口径：组织表只服务这两类人员（school_admin 无档案表）
Gender = str  # 'male' / 'female' / 'other'，由 CHECK 约束收口


class OrgClass(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """班级（ORG-06：入学年份=届身份 + 当前年级可流转；班主任/辅导员的权威源）。

    - `entry_year` 入学年份：届身份，建档后不可改（改=换届，删了重建）；
    - `grade` 当前年级：学年流转批量刷新的当前态标签；
    - 唯一 `(tenant_id, entry_year, name)`：同届班名唯一，跨届同名共存；
      `(当前年级, 班名)` 不设唯一（简报 D1：先建新届再流转不得撞名）；
    - `status`：active/archived——归档行永不删除（毕业届保留可查）。
    """

    __tablename__ = "classes"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "entry_year", "name", name="uq_classes_tenant_entry_year_name"
        ),
        # 供 students / teacher_class_courses 做复合外键 (class_id, tenant_id)
        UniqueConstraint("id", "tenant_id", name="uq_classes_id_tenant_id"),
        CheckConstraint("status IN ('active', 'archived')", name="ck_classes_status_valid"),
        CheckConstraint("entry_year BETWEEN 1970 AND 2100", name="ck_classes_entry_year_range"),
        ForeignKeyConstraint(
            ["head_teacher_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_classes_head_teacher_id_users",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["counselor_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_classes_counselor_id_users",
            ondelete="SET NULL",
        ),
    )

    entry_year: Mapped[int] = mapped_column(nullable=False)
    grade: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    head_teacher_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    counselor_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))


class Department(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """部门/科室树（A30）：邻接表 + id 物化路径。

    `path` 形如 `/{root_id}/{child_id}/…/{own_id}`（以自身 id 结尾）：
    子树查询 `path LIKE '{自身 path}/%'`；改名不动 path；移动子树由服务层重写。
    根部门（parent_id IS NULL）同级唯一性由部分唯一索引保证
    （迁移 a4c8e2f6b1d3 的 `uq_departments_tenant_root_name`）。
    """

    __tablename__ = "departments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "parent_id", "name", name="uq_departments_tenant_id_parent_id_name"
        ),
        UniqueConstraint("id", "tenant_id", name="uq_departments_id_tenant_id"),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_departments_status_valid"),
        CheckConstraint("parent_id <> id", name="ck_departments_parent_not_self"),
        # 根部门同级唯一（唯一约束视 NULL 为互异，覆盖不到）
        Index(
            "uq_departments_tenant_root_name",
            "tenant_id",
            "name",
            unique=True,
            postgresql_where=text("parent_id IS NULL"),
        ),
        # 子树前缀查询（M3 父部门人群命中子部门）；pattern_ops 让 LIKE 'prefix%' 走索引
        Index(
            "ix_departments_path",
            "path",
            postgresql_ops={"path": "varchar_pattern_ops"},
        ),
        ForeignKeyConstraint(
            ["parent_id", "tenant_id"],
            ["departments.id", "departments.tenant_id"],
            name="fk_departments_parent_id_departments",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_departments_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
    )

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str | None] = mapped_column(String(32))
    parent_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    path: Mapped[str] = mapped_column(String(600), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")


class Course(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """课程（编码租户内唯一）。"""

    __tablename__ = "courses"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_courses_tenant_id_code"),
        # 供 teacher_class_courses 做复合外键 (course_id, tenant_id)
        UniqueConstraint("id", "tenant_id", name="uq_courses_id_tenant_id"),
    )

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)


class Teacher(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """教师档案（与 users 1:1；id_card_* 三件套由 id_card_service 读写，ORG-04）。"""

    __tablename__ = "teachers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_teachers_tenant_id_user_id"),
        UniqueConstraint("tenant_id", "employee_no", name="uq_teachers_tenant_id_employee_no"),
        # 供 teacher_class_courses 做复合外键 (teacher_id, tenant_id)
        UniqueConstraint("id", "tenant_id", name="uq_teachers_id_tenant_id"),
        # 身份证指纹同租唯一兜底（M2-ORG04 复审 M3）：未采集（NULL）不参与
        Index(
            "uq_teachers_idcard_hmac",
            "tenant_id",
            "id_card_hmac",
            unique=True,
            postgresql_where=text("id_card_hmac IS NOT NULL"),
        ),
        Index("ix_teachers_tenant_id_department_id", "tenant_id", "department_id"),
        CheckConstraint("age IS NULL OR age BETWEEN 16 AND 80", name="ck_teachers_age_range"),
        ForeignKeyConstraint(
            ["department_id", "tenant_id"],
            ["departments.id", "departments.tenant_id"],
            name="fk_teachers_department_id_departments",
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "gender IS NULL OR gender IN ('male', 'female', 'other')",
            name="ck_teachers_gender_valid",
        ),
        ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_teachers_user_id_users",
            ondelete="CASCADE",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    employee_no: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    age: Mapped[int | None] = mapped_column(SmallInteger)
    gender: Mapped[Gender | None] = mapped_column(String(8))
    position: Mapped[str | None] = mapped_column(String(64))
    # 部门归属（A30，供 M3 KB-09 人群判定）；可空 = 未归属
    department_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    id_card_enc: Mapped[str | None] = mapped_column(Text)
    id_card_hmac: Mapped[str | None] = mapped_column(String(64))
    id_card_mask: Mapped[str | None] = mapped_column(String(32))


class Student(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """学生档案（class_id 可空 = 未分班；id_card_* 三件套由 id_card_service 读写，ORG-04）。"""

    __tablename__ = "students"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_students_tenant_id_user_id"),
        UniqueConstraint("tenant_id", "student_no", name="uq_students_tenant_id_student_no"),
        # 身份证指纹同租唯一兜底（M2-ORG04 复审 M3）：未采集（NULL）不参与
        Index(
            "uq_students_idcard_hmac",
            "tenant_id",
            "id_card_hmac",
            unique=True,
            postgresql_where=text("id_card_hmac IS NOT NULL"),
        ),
        CheckConstraint(
            "gender IS NULL OR gender IN ('male', 'female', 'other')",
            name="ck_students_gender_valid",
        ),
        ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_students_user_id_users",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["class_id", "tenant_id"],
            ["classes.id", "classes.tenant_id"],
            name="fk_students_class_id_classes",
            ondelete="SET NULL",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    student_no: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    gender: Mapped[Gender | None] = mapped_column(String(8))
    class_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    id_card_enc: Mapped[str | None] = mapped_column(Text)
    id_card_hmac: Mapped[str | None] = mapped_column(String(64))
    id_card_mask: Mapped[str | None] = mapped_column(String(32))


class TeacherClassCourse(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """任教关系（教师 × 班级 × 课程 × 学期；class 范围行由此表与 classes 派生）。"""

    __tablename__ = "teacher_class_courses"
    __table_args__ = (
        UniqueConstraint(
            "teacher_id",
            "class_id",
            "course_id",
            "term",
            name="uq_teacher_class_courses_teacher_id_class_id_course_id_term",
        ),
        ForeignKeyConstraint(
            ["teacher_id", "tenant_id"],
            ["teachers.id", "teachers.tenant_id"],
            name="fk_teacher_class_courses_teacher_id_teachers",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["class_id", "tenant_id"],
            ["classes.id", "classes.tenant_id"],
            name="fk_teacher_class_courses_class_id_classes",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["course_id", "tenant_id"],
            ["courses.id", "courses.tenant_id"],
            name="fk_teacher_class_courses_course_id_courses",
            ondelete="CASCADE",
        ),
    )

    teacher_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    class_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    course_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    term: Mapped[str] = mapped_column(String(16), nullable=False)


class AcademicYearRollover(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """学年流转批次（ORG-06）：幂等键唯一 = 防重复执行的数据库防线。

    若校方「先建新届（高一）再流转」，二次执行会把新届班也升上去——
    `(tenant_id, idempotency_key)` 唯一拦截重复提交（服务层译 409）。
    mapping_json/archive_grades_json 留执行时映射快照（追溯口径与审计一致）。
    """

    __tablename__ = "academic_year_rollovers"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_academic_year_rollovers_tenant_id_idempotency_key",
        ),
        CheckConstraint(
            "advanced_count >= 0 AND archived_count >= 0",
            name="ck_academic_year_rollovers_counters_nonnegative",
        ),
        ForeignKeyConstraint(
            ["operator_user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_academic_year_rollovers_operator_user_id_users",
            ondelete="RESTRICT",
        ),
    )

    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_json: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)
    archive_grades_json: Mapped[list[Any]] = mapped_column(JSONB(), nullable=False)
    advanced_count: Mapped[int] = mapped_column(nullable=False, default=0)
    archived_count: Mapped[int] = mapped_column(nullable=False, default=0)
    operator_user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
