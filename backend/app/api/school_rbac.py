"""校方侧角色权限配置、自定义角色、用户↔角色绑定、MFA 重置与权限账号启停
（M2-UI2 + M2-UI3 部分）。

本模块只做鉴权、请求/响应形状与错误映射；判定与落库在
`app/rbac/admin.py`、`app/rbac/binding.py`、`app/personnel/mfa_admin.py`
与 `app/personnel/account_admin.py`：

- 角色配置要求 `school.role.manage`；删除角色另要 `school.role.delete`（决策 #6：拆角色与
  配权限分离，能改配置的人不一定能让人失去角色），删除预览只读仍走 `school.role.manage`；
- 用户↔角色绑定要求 `school.permission_account.manage`（TD-187d）：读/写同一个码；绑定/解绑
  下一请求即生效、不作废在线会话；操作者不能改自己的绑定（403 `BINDING_SELF_FORBIDDEN`），
  目标不许是超管账号、提交的角色不许含超管角色；
- MFA 重置要求 `school.personnel.mfa.reset`，上述权限码都是租户域权限（`realm="tenant"`）；
- 权限账号启停（TD-187e）与绑定共用 `school.permission_account.manage`：停用要理由、撤未撤销且
  未过期的刷新家族并轮换 token 版本，启用只恢复状态并清锁定；超管账号一律不走租户侧启停；
- 角色数据范围编辑（TD-187c）与权限替换同用 `school.role.manage`、同一列 `roles.version`；
  范围只可手工授 `school`/`self`——`class`/`course` 必须绑定到具体 id 而本端点没有「选哪个班」的
  入参通道，一律拒授并留痕（`course` 本期还不贡献可见性，TD-151）。
  关系同步维护的是**用户级**行，角色级同型行今天没有合法写入方；
- 删除自定义角色的契约同时带 `version` 与 `fingerprint`（TD-199）：前者挡"预览后角色权限/范围
  被改"、后者挡"预览后持有人集合被改"（绑定不推进角色版本）；
- 错误统一返回 `{code, message}`；版本冲突额外带最新角色/绑定快照，前端据此刷新而不是静默覆盖；
- 响应不回显登录名/邮箱/身份证/MFA 密钥等敏感字段。
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from app.auth.principal import Principal, require_permission
from app.db.repository import CursorError
from app.personnel import account_admin, mfa_admin
from app.personnel.account_admin import AccountAdminError
from app.personnel.mfa_admin import MfaAdminError
from app.rbac import admin, binding
from app.rbac.admin import (
    ROLE_CODE_PATTERN,
    ResetPreview,
    RoleAdminError,
    RoleDeletionPreview,
    RoleSummary,
    ScopeChangePreview,
)
from app.rbac.binding import BindingAdminError, BindingUpdate, UserBindings
from app.rbac.self_grant import SENSITIVE_SELF_GRANT_CODES

router = APIRouter(prefix="/school", tags=["school"])

RoleManagerDep = require_permission("school.role.manage", realm="tenant")
# 删除角色与配置权限分离（决策 #6）：`school.role.manage` 只到建/改/绑定，删角色要专用码。
RoleDeleterDep = require_permission("school.role.delete", realm="tenant")
MfaResetterDep = require_permission("school.personnel.mfa.reset", realm="tenant")
# 权限账号管理（TD-187d）：绑定/解绑与（TD-187e 的）启停共用这一个码。
AccountManagerDep = require_permission("school.permission_account.manage", realm="tenant")


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
    # 能否经绑定接口授出（TD-187d）：超管角色为 false。服务端下发，前端不按 code 硬编码。
    bindable: bool
    # 能否"恢复出厂"（TD-200）：服务端派生——**有出厂定义**且**角色可编辑**（两道门都过）。
    # 前端按它点亮"恢复出厂"，`is_system` 只用于展示"内置/自建"标签（两者今天的等价只是巧合）。
    # 注意它只管**两道硬门**（锁定 → `ROLE_IMMUTABLE`；无出厂定义 → `ROLE_DEFAULTS_UNAVAILABLE`）：
    # 真正执行仍要理由 ≥10 字、逐字 `confirm_role_code`、当前 `version`，
    # 且 `school_admin` 的 `school` 范围净增会触发自扩权确认（422 `SELF_GRANT_CONFIRMATION_REQUIRED`）。
    # 所以前端要按"确认流"处理，不要按"点了必成"处理。
    resettable: bool


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


class RoleScopePayload(BaseModel):
    """全量替换角色级数据范围（TD-187c）。

    `scope_types` 有意不用 `Literal` 收窄：**未知字符串字面量**必须走到服务层的
    `UNKNOWN_SCOPE_TYPE` 才有 `{code,message}` 的错误契约，Pydantic 先拦会换成 FastAPI 的数组形
    422（与 `RoleDeletePayload.confirm_role_code` 同口径）。非字符串元素（`[1]`/`[null]`）仍被
    Pydantic 挡——那是类型错误，不是业务判据，不必给它留到服务层。清单不设长度上限，与权限侧
    `permission_codes` 同界（审查建议加 `max_length`，未采纳：单侧加限会让两条同族写路径的入参
    契约不一致，而越界的代价只是一次内存去重）。**重复项在服务层去重后写入**，等价于只提交一次。
    **空清单合法**——解析层 fail-closed 返回空集，预览用 `empties_scope` 提示运营（简报 §13.2 决策④）。
    `version` 与权限编辑共用同一列 `roles.version`：一次权限保存会让另一侧的草稿 409，
    代价见 TD-202。
    """

    scope_types: list[str]
    version: int = Field(ge=0)
    reason: str | None = Field(default=None, max_length=500)
    self_grant_ack: bool = False


class RoleScopePreviewPayload(BaseModel):
    """范围替换预览：只提交候选清单，版本与 ack 属于写路径，这里不接受。"""

    scope_types: list[str]


class RoleScopeRowResponse(BaseModel):
    """角色上的一条范围行（`editable=false` 不由本端点写入，前端只展示不可改）。"""

    scope_type: str
    scope_id: str | None
    editable: bool


class ScopeChangePreviewResponse(BaseModel):
    """范围替换的事前影响面（预览与执行返回同一语义，执行走 `RoleResponse`）。"""

    version: int
    added: list[str]
    removed: list[str]
    affected_users: int
    empties_scope: bool
    retained_non_editable: list[str]
    newly_reaching_self: list[str]
    requires_confirmation: bool


class ResetPreviewResponse(BaseModel):
    """重置出厂的影响面（预览与执行返回同一形状）。"""

    version: int
    permissions_added: list[str]
    permissions_removed: list[str]
    scopes_added: list[str]
    scopes_removed: list[str]
    affected_users: int


class RoleResetPayload(BaseModel):
    """重置出厂请求：输入角色 code 二次确认 + 理由 + 版本 + 自扩权确认。"""

    confirm_role_code: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=500)
    version: int = Field(ge=0)
    # 自扩权显式确认（TD-194）：重置会同时重种权限与范围，范围侧对 `school_admin` 的持有人
    # 是可达路径（出厂范围含 `school`）。默认 false —— 缺省不确认即 422，不静默放行。
    self_grant_ack: bool = False


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
    """删除自定义角色请求：逐字确认角色 code + 理由（服务层再校验 ≥10）+ 双判据 CAS。

    `version` 挡"预览后角色权限/范围被改"，`fingerprint` 挡"预览后持有人集合被改"（TD-199）。
    指纹原样回传预览值即可——它是 SHA-256 十六进制，服务端只做相等比较，不解析内容。
    """

    # 有意比 `code` 的上界（63）宽：这个字段只做逐字相等比较，超长必然是"用错了"，要让它走到
    # 服务层才有 `{code,message}` 形状的 `ROLE_CODE_MISMATCH`；收成 63 会先撞 Pydantic 的数组形
    # 422，把错误契约打断。与 M2-UI2 的 `RoleResetPayload` 同界。
    confirm_role_code: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=500)
    version: int = Field(ge=0)
    # 必填：漏传就 422（Pydantic），而不是让服务层把 None 当成"指纹不匹配"给一个语义模糊的 409。
    fingerprint: str = Field(min_length=1, max_length=128)


class RoleDeletionPreviewResponse(BaseModel):
    """删除自定义角色的影响面（预览与执行返回同一形状）。"""

    role_code: str
    name: str
    version: int
    affected_users: int
    permission_codes: list[str]
    fingerprint: str


class BoundRoleResponse(BaseModel):
    """账号当前绑定的一个角色。"""

    id: UUID
    code: str
    name: str
    is_system: bool
    bindable: bool


class UserBindingsResponse(BaseModel):
    """一个账号的绑定快照 + 生效权限并集。"""

    user_id: UUID
    display_name: str | None
    user_type: str
    status: str
    roles_version: int
    roles: list[BoundRoleResponse]
    permission_codes: list[str]


class BindingUpdatePayload(BaseModel):
    """全量替换账号角色绑定。

    `role_ids` 的元素类型由 Pydantic 收窄（非 UUID 是类型错误，不是业务判据）；
    跨租/不存在的 id 是业务判据，交给服务层给同形 404 与拒绝审计。
    空清单合法（"收回全部权限"）；`version` 是读到的账号绑定版本（`users.roles_version`）。
    """

    role_ids: list[UUID]
    version: int = Field(ge=0)
    reason: str | None = Field(default=None, max_length=500)


class BindingUpdateResponse(UserBindingsResponse):
    """全量替换的结果：最新快照 + 角色/权限码差异。"""

    added_role_codes: list[str]
    removed_role_codes: list[str]
    added_permission_codes: list[str]
    removed_permission_codes: list[str]


class UserLookupResponse(BaseModel):
    """按登录名定位到的用户（不含登录名/邮箱/身份证/MFA 字段）。"""

    id: UUID
    display_name: str | None
    user_type: str
    status: str
    mfa_enabled: bool
    roles: list[str]


class BindableRoleResponse(BaseModel):
    """可经绑定接口授出的角色（TD-187g 决策 2）：**只有身份，没有权限码清单**。

    完整权限矩阵是 `school.role.manage` 的事；这条端点给只持
    `school.permission_account.manage` 的人用，不该顺手把矩阵递出去。
    """

    id: UUID
    code: str
    name: str
    is_system: bool


class PermissionAccountResponse(BaseModel):
    """权限账号列表的一行（TD-187g 决策 1）：不回显 `login_id`（学号/工号属敏感标识符）。"""

    id: UUID
    display_name: str | None
    user_type: str
    status: str
    role_codes: list[str]
    roles_version: int
    mfa_enabled: bool
    # 该账号是否持有校方超管角色（服务端派生）：这类账号的角色绑定由平台开通流程管理，
    # 租户内改绑必然 403 `BINDING_SUPER_ADMIN_FORBIDDEN`，前端据此把绑定入口关掉。
    is_super_admin: bool


class PermissionAccountPageResponse(BaseModel):
    """权限账号列表的一页（cursor 分页，与 `GET /school/teachers` 同形）。"""

    items: list[PermissionAccountResponse]
    next_cursor: str | None


class MfaResetPayload(BaseModel):
    """MFA 重置请求：理由必填（服务层再校验 ≥10 字符）。"""

    reason: str = Field(min_length=1, max_length=500)


class DeactivateAccountPayload(BaseModel):
    """停用账号请求。

    body 合法但理由缺失/空串/全空白/不足 10 字时，一律走服务层的
    `{code,message}` 形状 `DEACTIVATE_REASON_REQUIRED`，而非 FastAPI 的数组形 422。
    **缺整个 body 或 `reason` 类型不符**仍由 Pydantic 先拦（数组形 422）——那是框架
    层的类型错误，和"理由不合法"是两件事（全局统一形状登记 TD-226）。
    `max_length` 只防超大字符串进内存，与 MFA 侧同界。
    """

    reason: str = Field(default="", max_length=500)


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
        bindable=summary.bindable,
        resettable=summary.resettable,
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
        fingerprint=preview.fingerprint,
    )


def _bindings_response(bindings: UserBindings) -> UserBindingsResponse:
    return UserBindingsResponse(
        user_id=bindings.user_id,
        display_name=bindings.display_name,
        user_type=bindings.user_type,
        status=bindings.status,
        roles_version=bindings.roles_version,
        roles=[
            BoundRoleResponse(
                id=role.id,
                code=role.code,
                name=role.name,
                is_system=role.is_system,
                bindable=role.bindable,
            )
            for role in bindings.roles
        ],
        permission_codes=list(bindings.permission_codes),
    )


def _binding_update_response(update: BindingUpdate) -> BindingUpdateResponse:
    snapshot = _bindings_response(update.bindings)
    return BindingUpdateResponse(
        **snapshot.model_dump(),
        added_role_codes=list(update.added_role_codes),
        removed_role_codes=list(update.removed_role_codes),
        added_permission_codes=list(update.added_permission_codes),
        removed_permission_codes=list(update.removed_permission_codes),
    )


def _scope_preview_response(preview: ScopeChangePreview) -> ScopeChangePreviewResponse:
    return ScopeChangePreviewResponse(
        version=preview.version,
        added=list(preview.added),
        removed=list(preview.removed),
        affected_users=preview.affected_users,
        empties_scope=preview.empties_scope,
        retained_non_editable=list(preview.retained_non_editable),
        newly_reaching_self=list(preview.newly_reaching_self),
        requires_confirmation=preview.requires_confirmation,
    )


def _role_error(exc: RoleAdminError) -> HTTPException:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.latest is not None:
        detail["latest"] = _role_response(exc.latest).model_dump(mode="json")
    if exc.latest_preview is not None:
        # 删除预览过期（TD-199）：回带最新影响面，调用方刷新确认框后重试，
        # 不必先跳一次 GET 再回来。
        detail["latest_preview"] = _deletion_response(exc.latest_preview).model_dump(mode="json")
    return HTTPException(status_code=exc.http_status, detail=detail)


def _binding_error(exc: BindingAdminError) -> HTTPException:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.latest is not None:
        detail["latest"] = _bindings_response(exc.latest).model_dump(mode="json")
    return HTTPException(status_code=exc.http_status, detail=detail)


def _mfa_error(exc: MfaAdminError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status, detail={"code": exc.code, "message": exc.message}
    )


def _account_error(exc: AccountAdminError) -> HTTPException:
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


@router.get("/bindable-roles", response_model=list[BindableRoleResponse])
async def list_bindable_roles(
    account_manager: Annotated[Principal, AccountManagerDep],
) -> list[BindableRoleResponse]:
    """可经绑定接口授出的角色目录（TD-187g 决策 2）。

    要 `school.permission_account.manage` 而不是 `school.role.manage`：绑定页要的是
    "能选哪些角色"，不需要角色的权限码清单。超管角色不可绑，**不列**——
    列出来就是"能勾不能存"。

    异常:
        HTTPException: 403（缺权限码/主体 realm 不符）。
    """

    try:
        roles = await binding.list_bindable_roles(account_manager)
    except BindingAdminError as exc:
        raise _binding_error(exc) from exc
    return [
        BindableRoleResponse(
            id=role.id,
            code=role.code,
            name=role.name,
            is_system=role.is_system,
        )
        for role in roles
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


@router.get("/roles/{role_id}/scopes", response_model=list[RoleScopeRowResponse])
async def list_role_scopes(
    role_id: UUID,
    manager: Annotated[Principal, RoleManagerDep],
) -> list[RoleScopeRowResponse]:
    """角色当前数据范围行（含 `editable=false` 的只读行，供配置页渲染）。

    角色级 `class`/`course` 同型行今天没有合法写入方（`app/org/scopes_sync.py` 只维护**用户级**行），
    这里出现它们只可能是历史数据或手工 SQL；`course` 另因本期不贡献可见性而无效果。两者都只展示不可改，
    是否可编辑由服务端下发，不让前端按范围类型名再猜一份（TD-200 同口径）。

    异常:
        HTTPException: 404（角色不存在/跨租）。
    """

    try:
        rows = await admin.list_role_scopes(manager, role_id)
    except RoleAdminError as exc:
        raise _role_error(exc) from exc
    return [
        RoleScopeRowResponse(scope_type=row.scope_type, scope_id=row.scope_id, editable=row.editable)
        for row in rows
    ]


@router.post("/roles/{role_id}/scopes/preview", response_model=ScopeChangePreviewResponse)
async def preview_role_scopes(
    role_id: UUID,
    payload: RoleScopePreviewPayload,
    manager: Annotated[Principal, RoleManagerDep],
) -> ScopeChangePreviewResponse:
    """只读预览范围替换的影响面（不写库、不审计、不推高版本）。

    类型族校验与 `PUT` **同码同序**（未知类型先于不可手工授类型），否则运营会看到
    "预览说能过、保存被拒"。两处结构性差异：`PUT` 的"角色存在/可编辑"排在类型族之间（它要先
    `FOR UPDATE` 拿到行才能判定），预览不锁行、把类型族一次性放在角色查询之前。判据码集合不变。
    预览阶段被拒不落安全审计，反复试探由写路径的 `*.denied` 计数。

    异常:
        HTTPException: 404（角色不存在/跨租）、409（锁定角色）、
            422（`UNKNOWN_SCOPE_TYPE`、`SCOPE_TYPE_NOT_EDITABLE`）。
    """

    try:
        preview = await admin.preview_role_scopes(
            manager, role_id, scope_types=set(payload.scope_types)
        )
    except RoleAdminError as exc:
        raise _role_error(exc) from exc
    return _scope_preview_response(preview)


@router.put("/roles/{role_id}/scopes", response_model=RoleResponse)
async def replace_role_scopes(
    role_id: UUID,
    payload: RoleScopePayload,
    request: Request,
    manager: Annotated[Principal, RoleManagerDep],
) -> RoleResponse:
    """全量替换角色级数据范围（版本 CAS + 审计 diff + 范围版自扩权显式确认，TD-187c）。

    只替换 `school`/`self` 两类；`class`/`course` 提交即 422 并留安全审计（这两类必须绑定具体
    班级/课程 id，本端点没有那个入参通道，写下去是解析层读不懂的脏数据，见简报 §13.2 决策③）。
    空清单合法，效果是该角色持有人看到空集。

    异常:
        HTTPException: 404（角色不存在/跨租）、409（锁定角色/版本冲突/类型与库 CHECK 漂移）、
            422（`UNKNOWN_SCOPE_TYPE`、`SCOPE_TYPE_NOT_EDITABLE`、
            `SELF_GRANT_CONFIRMATION_REQUIRED` 或未填理由）。
    """

    try:
        summary = await admin.replace_role_scopes(
            manager,
            role_id,
            scope_types=set(payload.scope_types),
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

    权限与范围两个维度都过自扩权守卫（TD-194）：重置 `school_admin` 会把它自己的范围
    净增回 `school`（出厂范围含全校），未带 `self_grant_ack` 的重置现在 422。

    异常:
        HTTPException: 404、409（锁定/版本冲突/无出厂定义）、
            422（角色名或理由不合格、`SELF_GRANT_CONFIRMATION_REQUIRED`）。
    """

    try:
        preview = await admin.reset_role(
            manager,
            role_id,
            confirm_role_code=payload.confirm_role_code,
            reason=payload.reason,
            version=payload.version,
            request_id=getattr(request.state, "request_id", None),
            self_grant_ack=payload.self_grant_ack,
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
    `version` 与 `fingerprint` 是两套判据（TD-199）：前者挡预览后角色配置被改，
    后者挡预览后持有人集合被改；指纹过期回 409 `ROLE_DELETE_PREVIEW_STALE` + 最新影响面。

    异常:
        HTTPException: 403（缺 `school.role.delete`）、404（不存在/跨租）、
            409（内置角色不可删/版本冲突/影响面已变化）、422（确认码不符或理由不足 10 字符）。
    """

    try:
        preview = await admin.delete_role(
            deleter,
            role_id,
            confirm_role_code=payload.confirm_role_code,
            reason=payload.reason,
            version=payload.version,
            fingerprint=payload.fingerprint,
            request_id=getattr(request.state, "request_id", None),
        )
    except RoleAdminError as exc:
        raise _role_error(exc) from exc
    return _deletion_response(preview)


@router.get("/permission-accounts", response_model=PermissionAccountPageResponse)
async def list_permission_accounts(
    account_manager: Annotated[Principal, AccountManagerDep],
    limit: Annotated[int, Query(ge=1, le=binding.MAX_PAGE_SIZE)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> PermissionAccountPageResponse:
    """列出本校权限账号（TD-187g 决策 1）。

    按账号类型过滤（教师 + 校方管理员，与绑定写路径同一集合），**不按人员数据范围裁剪**——
    权限账号管理是租户级操作，按 `school`/`self` 裁会让管理员看不见自己要管的账号。
    `deleted` 的账号不列。不回显 `login_id`。

    异常:
        HTTPException: 400（游标无效）、403（缺权限码/主体 realm 不符）。
    """

    try:
        page = await binding.list_permission_accounts(
            account_manager, limit=limit, cursor=cursor
        )
    except BindingAdminError as exc:
        raise _binding_error(exc) from exc
    except CursorError as exc:
        raise HTTPException(status_code=400, detail="分页游标无效") from exc
    return PermissionAccountPageResponse(
        items=[
            PermissionAccountResponse(
                id=item.id,
                display_name=item.display_name,
                user_type=item.user_type,
                status=item.status,
                role_codes=list(item.role_codes),
                roles_version=item.roles_version,
                mfa_enabled=item.mfa_enabled,
                is_super_admin=item.is_super_admin,
            )
            for item in page.items
        ],
        next_cursor=page.next_cursor,
    )


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


@router.get("/users/{user_id}/roles", response_model=UserBindingsResponse)
async def get_user_roles(
    user_id: UUID,
    account_manager: Annotated[Principal, AccountManagerDep],
) -> UserBindingsResponse:
    """该账号当前绑定的角色与生效权限并集（只读，不审计成功查询）。

    `roles_version` 是下一次 `PUT` 必须回传的绑定版本；`roles[].bindable` 由服务端下发
    （超管角色为 false），绑定页据此过滤，避免"能选中、提交必 422"的死胡同。

    异常:
        HTTPException: 403（缺 `school.permission_account.manage`）、404（不存在/跨租/已删除）。
    """

    try:
        snapshot = await binding.get_user_bindings(account_manager, user_id)
    except BindingAdminError as exc:
        raise _binding_error(exc) from exc
    return _bindings_response(snapshot)


@router.put("/users/{user_id}/roles", response_model=BindingUpdateResponse)
async def replace_user_roles(
    user_id: UUID,
    payload: BindingUpdatePayload,
    request: Request,
    account_manager: Annotated[Principal, AccountManagerDep],
) -> BindingUpdateResponse:
    """全量替换账号的角色绑定集（`users.roles_version` CAS + 差异审计）。

    绑定/解绑**下一个请求即生效**，不作废在线会话（权限每请求从库里重读）。
    操作者不能改自己的绑定（403 `BINDING_SELF_FORBIDDEN`）；目标不许是超管账号、
    提交的角色不许含超管角色；空清单合法（收回全部权限）。

    异常:
        HTTPException: 403（缺权限码/自绑/目标超管）、404（目标或角色不存在/跨租）、
            409（`BINDING_VERSION_CONFLICT`，带最新绑定快照）、
            422（目标账号类型不可绑/超管角色不可绑）。
    """

    try:
        update = await binding.replace_user_bindings(
            account_manager,
            user_id,
            role_ids=list(payload.role_ids),
            version=payload.version,
            reason=payload.reason,
            request_id=getattr(request.state, "request_id", None),
        )
    except BindingAdminError as exc:
        raise _binding_error(exc) from exc
    return _binding_update_response(update)


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


@router.post("/users/{user_id}/deactivate", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_account(
    user_id: UUID,
    payload: DeactivateAccountPayload,
    request: Request,
    account_manager: Annotated[Principal, AccountManagerDep],
) -> Response:
    """停用账号并撤销其活跃会话（旧 access/refresh 立即失效，重新登录被拒）。

    `reason` 去空白后不足 10 字 → 422 `DEACTIVATE_REASON_REQUIRED`；不能停用自己、
    不能停用超管账号、只能停教师/校方管理员（学生 422）；重复停用 409。
    拒绝路径写 `school.user.status_denied`。

    异常:
        HTTPException: 403（缺权限码/realm 不符/目标超管）、404（不存在/跨租/已删除）、
            409（停用自己/已停用）、422（理由过短 `DEACTIVATE_REASON_REQUIRED`、
            目标类型不是教师或校方管理员 `ACCOUNT_TARGET_NOT_ALLOWED`）。
    """

    try:
        await account_admin.deactivate_user(
            account_manager,
            user_id,
            reason=payload.reason,
            request_id=getattr(request.state, "request_id", None),
        )
    except AccountAdminError as exc:
        raise _account_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/users/{user_id}/activate", status_code=status.HTTP_204_NO_CONTENT)
async def activate_account(
    user_id: UUID,
    request: Request,
    account_manager: Annotated[Principal, AccountManagerDep],
) -> Response:
    """重新启用已停用账号并清空失败计数与锁定；不动 MFA、角色绑定与令牌版本。

    启用不需要理由，但目标类型判据与停用同一个入口、同码同形：学生账号 422。

    异常:
        HTTPException: 403（缺权限码/realm 不符/目标超管）、404（不存在/跨租/已删除）、
            409（账号未停用/已启用）、422（目标类型不是教师或校方管理员，两个方向同码
            `ACCOUNT_TARGET_NOT_ALLOWED`）。
    """

    try:
        await account_admin.activate_user(
            account_manager,
            user_id,
            request_id=getattr(request.state, "request_id", None),
        )
    except AccountAdminError as exc:
        raise _account_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
