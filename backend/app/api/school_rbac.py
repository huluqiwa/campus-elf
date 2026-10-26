"""校方侧角色权限配置与 MFA 管理员重置（M2-UI2：TD-137/TD-143/TD-126）。

本模块只做鉴权、请求/响应形状与错误映射；判定与落库在
`app/rbac/admin.py` 与 `app/personnel/mfa_admin.py`：

- 角色配置要求 `school.role.manage`；MFA 重置要求 `school.personnel.mfa.reset`，
  两者都是租户域权限（`realm="tenant"`）；
- 错误统一返回 `{code, message}`；版本冲突额外带最新角色快照，前端据此刷新而不是静默覆盖；
- 响应不回显登录名/邮箱/身份证/MFA 密钥等敏感字段。
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from app.auth.principal import Principal, require_permission
from app.personnel import mfa_admin
from app.personnel.mfa_admin import MfaAdminError
from app.rbac import admin
from app.rbac.admin import ResetPreview, RoleAdminError, RoleSummary

router = APIRouter(prefix="/school", tags=["school"])

RoleManagerDep = require_permission("school.role.manage", realm="tenant")
MfaResetterDep = require_permission("school.personnel.mfa.reset", realm="tenant")


class RoleResponse(BaseModel):
    """角色列表项（含权限码、版本与绑定用户数）。"""

    id: UUID
    code: str
    name: str
    is_system: bool
    editable: bool
    lock_reason: str | None
    version: int
    defaults_applied: bool
    user_count: int
    permission_codes: list[str]


class PermissionResponse(BaseModel):
    """租户域权限项（供前端按 module 分组）。"""

    code: str
    module: str
    action: str
    description: str


class RolePermissionPayload(BaseModel):
    """全量替换角色权限：`version` 为读出时的角色版本（乐观锁）。"""

    permission_codes: list[str]
    version: int = Field(ge=0)
    reason: str | None = Field(default=None, max_length=500)


class ResetPreviewResponse(BaseModel):
    """重置出厂的影响面（预览与执行返回同一形状）。"""

    version: int
    permissions_added: list[str]
    permissions_removed: list[str]
    scopes_added: list[str]
    scopes_removed: list[str]
    affected_users: int


class RoleResetPayload(BaseModel):
    """重置出厂请求：输入角色 code 二次确认 + 理由 + 版本。"""

    confirm_role_code: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=500)
    version: int = Field(ge=0)


class UserLookupResponse(BaseModel):
    """按登录名定位到的用户（不含登录名/邮箱/身份证/MFA 字段）。"""

    id: UUID
    display_name: str | None
    user_type: str
    status: str
    mfa_enabled: bool
    roles: list[str]


class MfaResetPayload(BaseModel):
    """MFA 重置请求：理由必填（服务层再校验 ≥10 字符）。"""

    reason: str = Field(min_length=1, max_length=500)


def _role_response(summary: RoleSummary) -> RoleResponse:
    return RoleResponse(
        id=summary.id,
        code=summary.code,
        name=summary.name,
        is_system=summary.is_system,
        editable=summary.editable,
        lock_reason=summary.lock_reason,
        version=summary.version,
        defaults_applied=summary.defaults_applied,
        user_count=summary.user_count,
        permission_codes=list(summary.permission_codes),
    )


def _preview_response(preview: ResetPreview) -> ResetPreviewResponse:
    return ResetPreviewResponse(
        version=preview.version,
        permissions_added=list(preview.permissions_added),
        permissions_removed=list(preview.permissions_removed),
        scopes_added=list(preview.scopes_added),
        scopes_removed=list(preview.scopes_removed),
        affected_users=preview.affected_users,
    )


def _role_error(exc: RoleAdminError) -> HTTPException:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.latest is not None:
        detail["latest"] = _role_response(exc.latest).model_dump(mode="json")
    return HTTPException(status_code=exc.http_status, detail=detail)


def _mfa_error(exc: MfaAdminError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status, detail={"code": exc.code, "message": exc.message}
    )


@router.get("/roles", response_model=list[RoleResponse])
async def list_roles(
    manager: Annotated[Principal, RoleManagerDep],
) -> list[RoleResponse]:
    """本校角色列表（含权限码、版本、绑定用户数）。

    异常:
        HTTPException: 403（主体 realm 不符）。
    """

    try:
        summaries = await admin.list_roles(manager)
    except RoleAdminError as exc:
        raise _role_error(exc) from exc
    return [_role_response(summary) for summary in summaries]


@router.get("/permissions", response_model=list[PermissionResponse])
async def list_permissions(
    _manager: Annotated[Principal, RoleManagerDep],
) -> list[PermissionResponse]:
    """租户域权限目录（school/campus），供角色配置页分组渲染。"""

    return [
        PermissionResponse(
            code=entry.code,
            module=entry.module,
            action=entry.action,
            description=entry.description,
        )
        for entry in admin.list_permissions()
    ]


@router.put("/roles/{role_id}/permissions", response_model=RoleResponse)
async def replace_role_permissions(
    role_id: UUID,
    payload: RolePermissionPayload,
    request: Request,
    manager: Annotated[Principal, RoleManagerDep],
) -> RoleResponse:
    """全量替换角色权限（版本锁 + 审计 diff）。

    异常:
        HTTPException: 404（角色不存在/跨租）、409（锁定角色/版本冲突/目录漂移）、
            422（非法权限码，或非超管试图授予 `school.role.manage`）。
    """

    try:
        summary = await admin.replace_role_permissions(
            manager,
            role_id,
            permission_codes=payload.permission_codes,
            version=payload.version,
            reason=payload.reason,
            request_id=getattr(request.state, "request_id", None),
        )
    except RoleAdminError as exc:
        raise _role_error(exc) from exc
    return _role_response(summary)


@router.post("/roles/{role_id}/reset/preview", response_model=ResetPreviewResponse)
async def preview_role_reset(
    role_id: UUID,
    manager: Annotated[Principal, RoleManagerDep],
) -> ResetPreviewResponse:
    """只读预览重置出厂的影响面（不写库、不审计）。

    异常:
        HTTPException: 404（角色不存在/跨租）、409（锁定角色或无出厂定义）。
    """

    try:
        preview = await admin.preview_role_reset(manager, role_id)
    except RoleAdminError as exc:
        raise _role_error(exc) from exc
    return _preview_response(preview)


@router.post("/roles/{role_id}/reset", response_model=ResetPreviewResponse)
async def reset_role(
    role_id: UUID,
    payload: RoleResetPayload,
    request: Request,
    manager: Annotated[Principal, RoleManagerDep],
) -> ResetPreviewResponse:
    """把角色恢复出厂：权限与数据范围一并重种，写审计后返回执行后的影响面。

    异常:
        HTTPException: 404、409（锁定/版本冲突/无出厂定义）、422（角色名或理由不合格）。
    """

    try:
        preview = await admin.reset_role(
            manager,
            role_id,
            confirm_role_code=payload.confirm_role_code,
            reason=payload.reason,
            version=payload.version,
            request_id=getattr(request.state, "request_id", None),
        )
    except RoleAdminError as exc:
        raise _role_error(exc) from exc
    return _preview_response(preview)


@router.get("/users/lookup", response_model=UserLookupResponse)
async def lookup_user(
    login_id: Annotated[str, Query(min_length=1, max_length=254)],
    request: Request,
    resetter: Annotated[Principal, MfaResetterDep],
) -> UserLookupResponse:
    """按登录名精确定位本校用户（查询留审计，便于追查枚举）。

    异常:
        HTTPException: 404（不存在/跨租）、422（登录名为空）。
    """

    try:
        user = await mfa_admin.lookup_user_by_login_id(
            resetter, login_id, request_id=getattr(request.state, "request_id", None)
        )
    except MfaAdminError as exc:
        raise _mfa_error(exc) from exc
    return UserLookupResponse(
        id=user.id,
        display_name=user.display_name,
        user_type=user.user_type,
        status=user.status,
        mfa_enabled=user.mfa_enabled,
        roles=list(user.roles),
    )


@router.post("/users/{user_id}/mfa/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_user_mfa(
    user_id: UUID,
    payload: MfaResetPayload,
    request: Request,
    resetter: Annotated[Principal, MfaResetterDep],
) -> Response:
    """清空目标用户 MFA 绑定并撤销其全部会话（密码不变）。

    异常:
        HTTPException: 403（目标超管）、404（不存在/跨租）、409（自己/未启用 MFA）、
            422（理由过短）。
    """

    try:
        await mfa_admin.reset_user_mfa(
            resetter,
            user_id,
            reason=payload.reason,
            request_id=getattr(request.state, "request_id", None),
        )
    except MfaAdminError as exc:
        raise _mfa_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
