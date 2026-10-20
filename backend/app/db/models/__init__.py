"""数据模型包：导入所有模型，保证 Alembic autogenerate 能发现全部表。"""

from app.db.models.audit import AuditLog
from app.db.models.identity import RefreshToken, User
from app.db.models.onboarding import SchoolReviewRecord, TenantRegistrationRequest
from app.db.models.rbac import (
    PERMISSION_CATALOG,
    DataScope,
    Permission,
    PlatformRole,
    PlatformRolePermission,
    PlatformUserRole,
    Role,
    RolePermission,
    UserRole,
)
from app.db.models.tenant import PlatformUser, Tenant

__all__ = [
    "PERMISSION_CATALOG",
    "AuditLog",
    "DataScope",
    "Permission",
    "PlatformRole",
    "PlatformRolePermission",
    "PlatformUser",
    "PlatformUserRole",
    "RefreshToken",
    "Role",
    "RolePermission",
    "SchoolReviewRecord",
    "Tenant",
    "TenantRegistrationRequest",
    "User",
    "UserRole",
]
