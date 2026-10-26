"""RBAC 模型：权限点、角色、角色-权限、用户-角色、数据范围。

设计要点（见 `docs/design-briefs/M1-tenant-identity.md` §3.1/§3.4）：
- `permissions` 是**全局目录**（平台级，无 tenant_id）；
- 校方角色 `roles` 与关联表都带 `tenant_id NOT NULL`，以便 RLS 统一生效；
- 平台角色 `platform_roles` + `platform_role_permissions` + `platform_user_roles` 不带 tenant_id。
"""

from typing import Literal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

ScopeType = Literal["school", "class", "course", "self"]


class Permission(UUIDPrimaryKeyMixin, Base):
    """权限点目录（全局）。"""

    __tablename__ = "permissions"
    __table_args__ = (UniqueConstraint("code", name="uq_permissions_code"),)

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    module: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(String(200))


class Role(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """校方角色（每租户一套，`is_system` 标记内置角色）。"""

    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_roles_tenant_id_code"),
        # 供子表做复合外键 (role_id, tenant_id)：保证父子同租
        UniqueConstraint("id", "tenant_id", name="uq_roles_id_tenant_id"),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_system: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    # 出厂默认（权限+数据范围）是否已初始化；false 时同步/开通补一次，之后清空不再回填（TD-150）。
    defaults_applied: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    # 权限/范围的乐观锁；管理接口按版本号拒绝并发覆盖（TD-137）。
    version: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")


class RolePermission(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """角色-权限（带 tenant_id，并用复合外键保证与角色同租）。"""

    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint(
            "role_id", "permission_id", name="uq_role_permissions_role_id_permission_id"
        ),
        ForeignKeyConstraint(
            ["role_id", "tenant_id"],
            ["roles.id", "roles.tenant_id"],
            name="fk_role_permissions_role_id_tenant_id_roles",
            ondelete="CASCADE",
        ),
    )

    role_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    permission_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False
    )


class UserRole(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """用户-角色（多角色；复合外键保证用户与角色都与行同租）。"""

    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_roles_user_id_role_id"),
        ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_user_roles_user_id_tenant_id_users",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["role_id", "tenant_id"],
            ["roles.id", "roles.tenant_id"],
            name="fk_user_roles_role_id_tenant_id_roles",
            ondelete="CASCADE",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    role_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)


class DataScope(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """数据范围授权：可授给角色或用户；`scope_id` 为班级/课程 id（school/self 为空）。"""

    __tablename__ = "data_scopes"
    __table_args__ = (
        Index("ix_data_scopes_role_id", "role_id"),
        Index("ix_data_scopes_user_id", "user_id"),
        ForeignKeyConstraint(
            ["role_id", "tenant_id"],
            ["roles.id", "roles.tenant_id"],
            name="fk_data_scopes_role_id_tenant_id_roles",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_data_scopes_user_id_tenant_id_users",
            ondelete="CASCADE",
        ),
        # 恰好绑定角色或用户之一；class/course 必须带 scope_id
        CheckConstraint("(role_id IS NOT NULL) <> (user_id IS NOT NULL)", name="scope_binding"),
        CheckConstraint(
            "scope_type IN ('school', 'class', 'course', 'self')", name="scope_type_valid"
        ),
        CheckConstraint(
            "(scope_type IN ('class', 'course')) = (scope_id IS NOT NULL)",
            name="scope_id_required_for_class_course",
        ),
        # class 范围行的数据库完整性（M2-ORG01-03 D1）：同一用户对同一班级只允许一行。
        # 迁移 c9d5f1e3a7b9 创建同名部分唯一索引；同步函数增删，这里是最后防线。
        Index(
            "uq_data_scopes_user_class",
            "user_id",
            "scope_id",
            unique=True,
            postgresql_where=text("scope_type = 'class' AND user_id IS NOT NULL"),
        ),
    )

    role_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    user_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))


class PlatformRole(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """平台角色（SaaS 超管/运营/审计）。"""

    __tablename__ = "platform_roles"
    __table_args__ = (UniqueConstraint("code", name="uq_platform_roles_code"),)

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_system: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")
    defaults_applied: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )


class PlatformRolePermission(UUIDPrimaryKeyMixin, Base):
    """平台角色-权限。"""

    __tablename__ = "platform_role_permissions"
    __table_args__ = (
        UniqueConstraint(
            "platform_role_id", "permission_id",
            name="uq_platform_role_permissions_platform_role_id_permission_id",
        ),
    )

    platform_role_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("platform_roles.id", ondelete="CASCADE"), nullable=False
    )
    permission_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False
    )


class PlatformUserRole(UUIDPrimaryKeyMixin, Base):
    """平台用户-角色。"""

    __tablename__ = "platform_user_roles"
    __table_args__ = (
        UniqueConstraint(
            "platform_user_id", "platform_role_id",
            name="uq_platform_user_roles_platform_user_id_platform_role_id",
        ),
    )

    platform_user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("platform_users.id", ondelete="CASCADE"), nullable=False
    )
    platform_role_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("platform_roles.id", ondelete="CASCADE"), nullable=False
    )


# ---------------------------------------------------------------------------
# 内置角色「出厂默认」定义表（M1-E1，代码所有的数据）
#
# 为什么入库而不是抄进 SQL：`provision_tenant()` / `seed_builtin_roles()` 是数据库函数，
# 开通新租户时要建内置角色并授默认权限。把权限码清单抄进函数体就会有两份真相
# （`seeds.py` 一份、SQL 一份）——新增一个权限码却忘了改函数，新租户静默少一条权限。
# 现在函数只读这两张表，`app/rbac` 的同步每次按 `seeds.py` 整表重写它们。
#
# 它们**不是**运营配置：实际生效的映射在 `roles`/`role_permissions`，运营改完同步不会覆盖。
# ---------------------------------------------------------------------------
class RbacBuiltinRole(Base):
    """内置角色清单（realm + 角色码 + 显示名）。"""

    __tablename__ = "rbac_builtin_roles"
    __table_args__ = (
        PrimaryKeyConstraint("realm", "code"),
        CheckConstraint("realm IN ('platform', 'tenant')", name="realm_valid"),
    )

    realm: Mapped[str] = mapped_column(String(16), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)


class RbacBuiltinRoleDefault(Base):
    """内置角色的出厂默认权限映射（按 realm + 角色码 + 权限码）。"""

    __tablename__ = "rbac_builtin_role_defaults"
    __table_args__ = (
        PrimaryKeyConstraint("realm", "role_code", "permission_code"),
        ForeignKeyConstraint(
            ["permission_code"],
            ["permissions.code"],
            name="fk_rbac_builtin_role_defaults_permission_code_permissions",
            ondelete="CASCADE",
        ),
        CheckConstraint("realm IN ('platform', 'tenant')", name="realm_valid"),
    )

    realm: Mapped[str] = mapped_column(String(16), nullable=False)
    role_code: Mapped[str] = mapped_column(String(64), nullable=False)
    permission_code: Mapped[str] = mapped_column(String(64), nullable=False)


class RbacBuiltinRoleScopeDefault(Base):
    """内置角色的出厂默认数据范围（M1-E2，口径 ⑨）。

    与 `rbac_builtin_role_defaults` 同性质：**代码所有**的定义表，不是运营配置。
    为什么要有它：`seed_builtin_roles(uuid)` 是数据库函数，新租户开通时要写 `data_scopes`；
    把范围清单抄进函数体就会出现"Python 一份、SQL 一份"，改默认时静默漏改。

    `realm` 只允许 `tenant`：`data_scopes` 是租户级表（绑角色/用户 + tenant_id），
    平台角色没有租户内数据范围的概念，写进来就是脏数据。
    """

    __tablename__ = "rbac_builtin_role_scope_defaults"
    __table_args__ = (
        PrimaryKeyConstraint("realm", "role_code", "scope_type"),
        CheckConstraint("realm = 'tenant'", name="realm_only_tenant"),
        CheckConstraint(
            "scope_type IN ('school', 'class', 'course', 'self')", name="scope_type_valid"
        ),
    )

    realm: Mapped[str] = mapped_column(String(16), nullable=False)
    role_code: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)


# 供种子数据与校验使用的最小权限目录（M1-A 只落目录，实际授权在 M1-E 细化）
PERMISSION_CATALOG: tuple[tuple[str, str, str], ...] = (
    ("platform.tenant.view", "platform", "view"),
    ("platform.tenant.review", "platform", "review"),
    ("platform.tenant.activate", "platform", "activate"),
    ("platform.tenant.export", "platform", "export"),
    ("platform.config.view", "platform", "view"),
    ("platform.audit.view", "platform", "view"),
    ("platform.audit.export", "platform", "export"),
    ("school.profile.view", "school", "view"),
    ("school.profile.edit", "school", "edit"),
    ("school.user.view", "school", "view"),
    ("school.user.create", "school", "create"),
    ("school.user.edit", "school", "edit"),
    ("school.user.delete", "school", "delete"),
    ("school.role.assign", "school", "assign"),
    ("school.kb.view", "school", "view"),
    ("school.kb.view_chunks", "school", "view"),
    ("school.leave.approve", "school", "approve"),
    ("school.audit.view", "school", "view"),
    ("campus.chat.use", "campus", "use"),
    ("campus.leave.submit", "campus", "submit"),
)
