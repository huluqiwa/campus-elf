"""校方侧角色权限配置、自定义角色与 MFA 管理员重置（M2-UI2 + M2-UI3 部分）。

本模块只做鉴权、请求/响应形状与错误映射；判定与落库在
`app/rbac/admin.py` 与 `app/personnel/mfa_admin.py`：

- 角色配置要求 `school.role.manage`；MFA 重置要求 `school.personnel.mfa.reset`，
  两者都是租户域权限（`realm="tenant"`）；
- 删除角色另要 `school.role.delete`（决策 #6：拆角色与配权限分离，
  能改配置的人不一定能让人失去角色）；删除预览只读，仍走 `school.role.manage`；
- 错误统一返回 `{code, message}`；版本冲突额外带最新角色快照，前端据此刷新而不是静默覆盖；
- 响应不回显登录名/邮箱/身份证/MFA 密钥等敏感字段。
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from app.auth.principal import Principal, require_permission
from app.personnel import mfa_admin
from app.personnel.mfa_admin import MfaAdminError
from app.rbac import admin
from app.rbac.admin import (
    ROLE_CODE_PATTERN,
    ResetPreview,
    RoleAdminError,
    RoleDeletionPreview,
    RoleSummary,
)
from app.rbac.self_grant import SENSITIVE_SELF_GRANT_CODES

router = APIRouter(prefix="/school", tags=["school"])

RoleManagerDep = require_permission("school.role.manage", realm="tenant")
# 删除角色与配置权限分离（决策 #6）：`school.role.manage` 只到建/改/绑定，删角色要专用码。
RoleDeleterDep = require_permission("school.role.delete", realm="tenant")
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
    # 自扩权敏感码（TD-191）：随目录一起下发，前端据此决定要不要弹确认。
    # 不在前端复刻一份清单——否则后端加一个敏感码，前端的提示就静默失效了。
    sensitive_self_grant: bool


class RolePermissionPayload(BaseModel):
    """全量替换角色权限：`version` 为读出时的角色版本（乐观锁）。"""

    permission_codes: list[str]
    version: int = Field(ge=0)
    reason: str | None = Field(default=None, max_length=500)
    # 自扩权显式确认（TD-191）：结果集触达自己所在角色且净增敏感码时必须为 true，
    # 默认 false —— 缺省不确认，前端不勾选即 422，而不是静默放行。
    self_grant_ack: bool = False


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


class RoleCreatePayload(BaseModel):
    """创建自定义角色请求。

    `code` 校验与库层 `ck_roles_code_format_valid` 同界（3..63，同一个正则）：若按列宽 64 写，
    64 字符会通过这里、在 INSERT 撞 CHECK 变 500 而不是 422（简报 §4.1）。
    初始权限二选一：给 `permission_codes`（默认空集），或 `copy_from_role_id` 复制同租户可编辑角色。
    **空清单与不给该字段等价**（复制模式下前端勾选框本来就是空的），`ROLE_INITIAL_SOURCE_AMBIGUOUS`
    只在"非空清单 + 复制源同时给"时触发。
    """

    code: str = Field(min_length=3, max_length=63, pattern=ROLE_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=100)
    permission_codes: list[str] = Field(default_factory=list)
    copy_from_role_id: UUID | None = None
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        # `min_length=1` 挡得住空串，挡不住 "   "：服务层还会再 strip，于是落库成一行没有名字的
        # 角色，运营只能靠 code 认。全空格在接口层就按非法处理。
        stripped = value.strip()
        if not stripped:
            raise ValueError("name 不能为空或全空白")
        return stripped


class RoleDeletePayload(BaseModel):
    """删除自定义角色请求：逐字确认角色 code + 理由（服务层再校验 ≥10）+ 版本 CAS。"""

    # 有意比 `code` 的上界（63）宽：这个字段只做逐字相等比较，超长必然是"用错了"，要让它走到
    # 服务层才有 `{code,message}` 形状的 `ROLE_CODE_MISMATCH`；收成 63 会先撞 Pydantic 的数组形
    # 422，把错误契约打断。与 M2-UI2 的 `RoleResetPayload` 同界。
    confirm_role_code: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=500)
    version: int = Field(ge=0)


class RoleDeletionPreviewResponse(BaseModel):
    """删除自定义角色的影响面（预览与执行返回同一形状）。"""

    role_code: str
    name: str
    version: int
    affected_users: int
    permission_codes: list[str]


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


def _deletion_response(preview: RoleDeletionPreview) -> RoleDeletionPreviewResponse:
    return RoleDeletionPreviewResponse(
        role_code=preview.role_code,
        name=preview.name,
        version=preview.version,
        affected_users=preview.affected_users,
        permission_codes=list(preview.permission_codes),
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
            sensitive_self_grant=entry.code in SENSITIVE_SELF_GRANT_CODES,
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
    """全量替换角色权限（版本锁 + 审计 diff + 自扩权显式确认）。

    异常:
        HTTPException: 404（角色不存在/跨租）、409（锁定角色/版本冲突/目录漂移）、
            422（非法权限码、非超管试图授予 `school.role.manage`、
            自扩权未确认 `SELF_GRANT_CONFIRMATION_REQUIRED` 或未填理由）。
    """

    try:
        summary = await admin.replace_role_permissions(
            manager,
            role_id,
            permission_codes=payload.permission_codes,
            version=payload.version,
            reason=payload.reason,
            request_id=getattr(request.state, "request_id", None),
            self_grant_ack=payload.self_grant_ack,
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


@router.post("/roles", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: RoleCreatePayload,
    request: Request,
    manager: Annotated[Principal, RoleManagerDep],
) -> RoleResponse:
    """创建自定义角色（`is_system=false`，本租户内）。

    异常:
        HTTPException: 409（`ROLE_CODE_TAKEN` 撞内置保留码或本租户既有码）、
            404（复制源角色不存在/跨租）、422（角色码格式、权限码非法或平台域码、
            复制源为锁定角色、非超管转授 `school.role.manage`）。
    """

    try:
        summary = await admin.create_role(
            manager,
            code=payload.code,
            name=payload.name,
            permission_codes=list(payload.permission_codes),
            copy_from_role_id=payload.copy_from_role_id,
            reason=payload.reason,
            request_id=getattr(request.state, "request_id", None),
        )
    except RoleAdminError as exc:
        raise _role_error(exc) from exc
    return _role_response(summary)


@router.post("/roles/{role_id}/delete/preview", response_model=RoleDeletionPreviewResponse)
async def preview_role_deletion(
    role_id: UUID,
    manager: Annotated[Principal, RoleManagerDep],
) -> RoleDeletionPreviewResponse:
    """只读预览删除自定义角色的影响面（不写库、不审计）。

    删除权限用 `school.role.manage` 而不是 `school.role.delete`：预览不改状态，
    与重置预览同口径；只有执行删除才要求专用删除码（决策 #6）。

    异常:
        HTTPException: 404（角色不存在/跨租）、409（内置角色不可删）。
    """

    try:
        preview = await admin.preview_role_deletion(manager, role_id)
    except RoleAdminError as exc:
        raise _role_error(exc) from exc
    return _deletion_response(preview)


@router.post("/roles/{role_id}/delete", response_model=RoleDeletionPreviewResponse)
async def delete_role(
    role_id: UUID,
    payload: RoleDeletePayload,
    request: Request,
    deleter: Annotated[Principal, RoleDeleterDep],
) -> RoleDeletionPreviewResponse:
    """硬删自定义角色：权限、数据范围、用户绑定随复合外键 CASCADE 一并清理。

    删除前的权限码集与受影响用户数先快照进审计——CASCADE 之后取不回，
    没有这份快照就无法回答「谁在什么时候失去了哪些权限」。

    异常:
        HTTPException: 403（缺 `school.role.delete`）、404（不存在/跨租）、
            409（内置角色不可删/版本冲突）、422（确认码不符或理由不足 10 字符）。
    """

    try:
        preview = await admin.delete_role(
            deleter,
            role_id,
            confirm_role_code=payload.confirm_role_code,
            reason=payload.reason,
            version=payload.version,
            request_id=getattr(request.state, "request_id", None),
        )
    except RoleAdminError as exc:
        raise _role_error(exc) from exc
    return _deletion_response(preview)


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
