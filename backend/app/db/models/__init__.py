"""数据模型包：导入所有模型，保证 Alembic autogenerate 能发现全部表。"""

from app.db.models.approval import (
    ApprovalAction,
    ApprovalRequest,
    ApprovalStep,
    ApprovalType,
)
from app.db.models.audit import AuditLog
from app.db.models.identity import (
    MfaRecoveryCode,
    PlatformMfaRecoveryCode,
    PlatformRefreshToken,
    RefreshToken,
    User,
)
from app.db.models.imports import ImportJob, ImportRowError
from app.db.models.kb import (
    KbBuildJob,
    KbChunk,
    KbDocument,
    KbDocumentVersion,
    KbIndexVersion,
    KbUploadBatch,
)
from app.db.models.onboarding import SchoolReviewRecord, TenantRegistrationRequest
from app.db.models.org import (
    Course,
    Department,
    OrgClass,
    Student,
    Teacher,
    TeacherClassCourse,
)
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
    "ApprovalAction",
    "ApprovalRequest",
    "ApprovalStep",
    "ApprovalType",
    "AuditLog",
    "Course",
    "DataScope",
    "Department",
    "ImportJob",
    "ImportRowError",
    "KbBuildJob",
    "KbChunk",
    "KbDocument",
    "KbDocumentVersion",
    "KbIndexVersion",
    "KbUploadBatch",
    "MfaRecoveryCode",
    "OrgClass",
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
    "Student",
    "SystemSetting",
    "Teacher",
    "TeacherClassCourse",
    "Tenant",
    "TenantRegistrationRequest",
    "User",
    "UserRole",
]
