"""数据模型包：导入所有模型，保证 Alembic autogenerate 能发现全部表。"""

from app.db.models.audit import AuditLog
from app.db.models.identity import (
    MfaRecoveryCode,
    PlatformMfaRecoveryCode,
    PlatformRefreshToken,
    RefreshToken,
    User,
)
from app.db.models.onboarding import SchoolReviewRecord, TenantRegistrationRequest
from app.db.models.rbac import (
    PERMISSION_CATALOG,
    DataScope,
    Permission,
    PlatformRole,
    PlatformRolePermission,
    PlatformUserRole,
    RbacBuiltinRole,
    RbacBuiltinRoleDefault,
    RbacBuiltinRoleScopeDefault,
    Role,
    RolePermission,
    UserRole,
)
from app.db.models.settings import SystemSetting
from app.db.models.tenant import PlatformUser, Tenant

__all__ = [
    "PERMISSION_CATALOG",
    "AuditLog",
    "DataScope",
    "MfaRecoveryCode",
    "Permission",
    "PlatformMfaRecoveryCode",
    "PlatformRefreshToken",
    "PlatformRole",
    "PlatformRolePermission",
    "PlatformUser",
    "PlatformUserRole",
    "RbacBuiltinRole",
    "RbacBuiltinRoleDefault",
    "RbacBuiltinRoleScopeDefault",
    "RefreshToken",
    "Role",
    "RolePermission",
    "SchoolReviewRecord",
    "SystemSetting",
    "Tenant",
    "TenantRegistrationRequest",
    "User",
    "UserRole",
]
