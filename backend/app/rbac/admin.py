"""角色权限配置（TD-137/TD-143）：列表、全量替换、重置出厂。

口径（`docs/design-briefs/M2-UI2-rbac-admin.md` §3/§4）：

- 只允许编辑两个内置角色（`school_admin`、`teacher_b_end`）；超管与 C 端角色锁定，
  避免把登录/对话/审批入口改坏或把超管自锁；
- 映射是**运营配置**：接口全量替换并写审计 diff；`roles.version` 乐观锁拒绝并发覆盖；
- 重置出厂恢复**权限与数据范围**两侧，执行前可预览影响面；
- `school.role.manage` 只能由校方超管本人授出（转授即等于无条件把本校全部权限交出去）；
- 自扩权（TD-191 收口）：结果集触达操作者自己所在角色、且净增敏感码时，必须带
  `self_grant_ack=true` + 理由（超管免打断）。`school.role.self_grant` 审计**放行与打断都写**
  ——放行时同事务，被打断时走独立事务（外层回滚会带走同事务的留痕）；
- 所有写入在租户连接内完成（RLS + 显式 tenant_id 过滤），跨租 role_id 与不存在同形 404；
- 被拒的写操作（锁定角色、非法转授）在**独立事务**写 `*.denied` 审计——外层事务会回滚，
  同事务写的拒绝留痕跟着一起消失。
"""

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import append_audit
from app.auth.principal import Principal
from app.db.session import db_session
from app.rbac.catalog import PERMISSION_CATALOG, TENANT_MODULES, PermissionDef, permission_def
from app.rbac.self_grant import SelfGrantExposure, evaluate_self_grant

logger = logging.getLogger(__name__)

# 本点可编辑的内置角色；其余角色只读（自定义角色 CRUD 属 M2-UI3）。
EDITABLE_ROLE_CODES: frozenset[str] = frozenset({"school_admin", "teacher_b_end"})
# 转授禁令（用户 2026-10-26 决策，见 `_assert_delegable`）。
NON_DELEGABLE_CODES: frozenset[str] = frozenset({"school.role.manage"})
SUPER_ADMIN_ROLE_CODE = "school_super_admin"
LOCKED_ROLE_REASONS: dict[str, str] = {
    "school_super_admin": "校方超管固定为本校 school+campus 全量权限，防止自锁；调整走平台流程",
    "student": "C 端学生基础角色锁定，避免改坏登录/对话/请假入口",
    "teacher": "C 端教师基础角色锁定，避免改坏登录/对话/请假入口",
}
_CUSTOM_ROLE_REASON = "自定义角色不在本期管理范围（M2-UI3）"
_SYSTEM_ROLE_REASON = "该内置角色不在本期可编辑范围"

TENANT_PERMISSIONS: tuple[PermissionDef, ...] = tuple(
    entry for entry in PERMISSION_CATALOG if entry.module in TENANT_MODULES
)


class RoleAdminError(RuntimeError):
    """角色配置的业务错误（映射为 HTTP 状态码）。"""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int,
        *,
        latest: "RoleSummary | None" = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.latest = latest


@dataclass(frozen=True, slots=True)
class RoleSummary:
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
    permission_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResetPreview:
    """重置出厂的影响面（预览与执行返回同一形状）。"""

    version: int
    permissions_added: tuple[str, ...]
    permissions_removed: tuple[str, ...]
    scopes_added: tuple[str, ...]
    scopes_removed: tuple[str, ...]
    affected_users: int


def _require_tenant(principal: Principal) -> UUID:
    """角色配置只接受租户主体。"""

    if principal.realm != "tenant" or principal.tenant_id is None:
        raise RoleAdminError("REALM_MISMATCH", "角色配置只在校方租户上下文内可用", 403)
    return principal.tenant_id


def _lock_reason(code: str, *, is_system: bool) -> str | None:
    """不可编辑角色给出人读原因；可编辑返回 None。"""

    if is_system and code in EDITABLE_ROLE_CODES:
        return None
    if code in LOCKED_ROLE_REASONS:
        return LOCKED_ROLE_REASONS[code]
    return _CUSTOM_ROLE_REASON if not is_system else _SYSTEM_ROLE_REASON


# 角色查询（列表与单行共用）：`rid` 传 None 即列全本校角色，传具体 id 即单行读。
# SQL 文本是常量，所有值都走绑定参数——不做字符串拼接，bandit B608 无需豁免。
_ROLE_SELECT_SQL = """
select
    r.id,
    r.code,
    r.name,
    r.is_system,
    r.defaults_applied,
    r.version,
    (
        select count(*) from public.user_roles ur
        where ur.role_id = r.id and ur.tenant_id = r.tenant_id
    )::int as user_count,
    coalesce(
        (
            select array_agg(p.code order by p.code)
            from public.role_permissions rp
            join public.permissions p on p.id = rp.permission_id
            where rp.role_id = r.id and rp.tenant_id = r.tenant_id
        ),
        array[]::text[]
    ) as permission_codes
from public.roles r
where r.tenant_id = cast(:tid as uuid)
  and (cast(:rid as uuid) is null or r.id = cast(:rid as uuid))
order by r.is_system desc, r.code
"""


def _to_summary(row: Any) -> RoleSummary:
    is_system = bool(row["is_system"])
    code = str(row["code"])
    return RoleSummary(
        id=UUID(str(row["id"])),
        code=code,
        name=str(row["name"]),
        is_system=is_system,
        editable=is_system and code in EDITABLE_ROLE_CODES,
        lock_reason=_lock_reason(code, is_system=is_system),
        version=int(row["version"]),
        defaults_applied=bool(row["defaults_applied"]),
        user_count=int(row["user_count"]),
        permission_codes=tuple(str(code) for code in row["permission_codes"]),
    )


async def list_roles(principal: Principal) -> list[RoleSummary]:
    """本校全部角色及其权限码、版本、绑定用户数（无 N+1 聚合）。"""

    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        rows = (
            await session.execute(text(_ROLE_SELECT_SQL), {"tid": tenant_id, "rid": None})
        ).mappings()
        return [_to_summary(row) for row in rows]


def list_permissions() -> list[PermissionDef]:
    """租户域权限目录（school/campus），按模块与码排序供前端分组。"""

    return sorted(TENANT_PERMISSIONS, key=lambda entry: (entry.module, entry.code))


def _validate_codes(codes: list[str]) -> None:
    """权限码必须是租户域目录内条目；平台域码与幽灵码都拒绝（422）。"""

    unknown = sorted(code for code in codes if permission_def(code) is None)
    if unknown:
        raise RoleAdminError(
            "UNKNOWN_PERMISSION_CODE", f"目录外的权限码：{unknown}", 422
        )
    leaked = sorted(
        code
        for code in codes
        if (entry := permission_def(code)) is not None and entry.module not in TENANT_MODULES
    )
    if leaked:
        raise RoleAdminError(
            "PLATFORM_PERMISSION_FORBIDDEN", f"租户角色不得引用平台域权限：{leaked}", 422
        )


def _assert_delegable(principal: Principal, codes: list[str]) -> None:
    """`school.role.manage` 只能由校方超管本人授出（422）。

    持有该码即可给任意可编辑角色写入任意 `school`/`campus` 码（realm 守卫只挡平台域），
    所以下一次请求就能把本校全部权限加进自己所在的角色——不需要任何人再审批。矩阵里
    校方管理员这一格是「△=可按需授予」，本函数把"谁能授这个码"收紧到超管。
    """

    if SUPER_ADMIN_ROLE_CODE in principal.roles:
        return
    blocked = sorted(NON_DELEGABLE_CODES.intersection(codes))
    if blocked:
        raise RoleAdminError(
            "ROLE_CODE_NOT_DELEGABLE", f"该权限码只能由校方超管授予：{blocked}", 422
        )


async def _audit_denied(
    principal: Principal,
    *,
    action: str,
    resource_id: str,
    reason: str,
    request_id: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    """被拒操作留痕（独立事务，best-effort）。

    服务层在写事务内 raise 时外层会整体回滚，同事务写的审计一起消失；拒绝本身是安全事件
    （谁试图改锁定角色/试图转授提权码），必须留下痕迹。审计故障不得改变拒绝结果。

    `extra` 用于判据本身需要上下文才可读的动作（自扩权要看到具体敏感码清单）；只允许判定
    用的码/布尔，PII 与角色名一律不进审计。
    """

    tenant_id = principal.tenant_id
    if tenant_id is None:
        return
    try:
        async with db_session(tenant_id=tenant_id) as session:
            await append_audit(
                session,
                action=action,
                resource_type="role",
                resource_id=resource_id,
                actor_realm="tenant",
                actor_id=principal.user_id,
                tenant_id=tenant_id,
                result="denied",
                metadata={"reason": reason, **(extra or {})},
                request_id=request_id,
            )
    except Exception as exc:
        # 拒绝仍然生效，只是失去一次留痕机会。
        # 与 `personnel.service._audit_denied` 同口径：只记异常类型，不用 exc_info——
        # SQLAlchemy 会把 SQL 与参数拼进异常字符串，开 traceback 等于把参数写进日志（红线）。
        logger.warning(
            "角色配置拒绝审计写入失败：%s.%s",
            type(exc).__module__,
            type(exc).__name__,
            extra={"action": action},
        )
        return


async def _role_row(
    session: AsyncSession, tenant_id: UUID, role_id: UUID, *, for_update: bool
) -> dict[str, Any] | None:
    sql = (
        "select id, code, name, is_system, defaults_applied, version "
        "from public.roles where id = :rid and tenant_id = :tid for update"
        if for_update
        else "select id, code, name, is_system, defaults_applied, version "
        "from public.roles where id = :rid and tenant_id = :tid"
    )
    row = (
        await session.execute(text(sql), {"rid": role_id, "tid": tenant_id})
    ).mappings().one_or_none()
    return None if row is None else dict(row)


async def _summary(session: AsyncSession, tenant_id: UUID, role_id: UUID) -> RoleSummary:
    row = (
        await session.execute(
            text(_ROLE_SELECT_SQL),
            {"rid": role_id, "tid": tenant_id},
        )
    ).mappings().one()
    return _to_summary(row)


def _assert_editable(role: dict[str, Any]) -> None:
    reason = _lock_reason(str(role["code"]), is_system=bool(role["is_system"]))
    if reason is not None:
        raise RoleAdminError("ROLE_IMMUTABLE", reason, 409)


async def _role_permission_codes(session: AsyncSession, tenant_id: UUID, role_id: UUID) -> set[str]:
    rows: list[Any] = list(
        (
            await session.execute(
                text(
                    "select p.code from public.role_permissions rp "
                    "join public.permissions p on p.id = rp.permission_id "
                    "where rp.role_id = :rid and rp.tenant_id = :tid"
                ),
                {"rid": role_id, "tid": tenant_id},
            )
        )
        .scalars()
        .all()
    )
    return {str(code) for code in rows}


async def _assert_codes_in_catalog(session: AsyncSession, codes: set[str]) -> None:
    """提交的每个码都必须能在 `permissions` 里查到（全量替换路径专用）。

    代码目录已加新码而 `sync_rbac_definitions()` 未跑时，按 code join 会**静默少挂几条**：
    全量替换会写成"提交 N 项、实际生效 N-k 项"——宁可 409 让运维去跑同步。
    重置路径不在这里探针：它的权限集来自库内的 `rbac_builtin_role_defaults`，
    一致性由 `reset_role` 的重种条数复核保证（见该函数内的 expected 对比）。
    """

    if not codes:
        return
    ordered = sorted(codes)
    known = int(
        (
            await session.execute(
                text(
                    "select count(*) from public.permissions "
                    "where code = any(cast(:codes as text[]))"
                ),
                {"codes": ordered},
            )
        ).scalar_one()
    )
    if known != len(ordered):
        raise RoleAdminError(
            "PERMISSION_CATALOG_OUT_OF_SYNC", "权限目录与数据库不一致，请先执行 RBAC 同步", 409
        )


async def _principal_role_ids(session: AsyncSession, tenant_id: UUID, user_id: UUID) -> set[str]:
    """操作者当前绑定的角色 id（字符串集合）。

    「触达自己」只能用库里的真实绑定判定，不能用 `principal.roles`（那是 code 且来自请求解析）
    近似：M2-UI3 的多角色绑定下，一个账号可能同时挂在内置角色和自定义角色上。
    """

    rows: list[Any] = list(
        (
            await session.execute(
                text(
                    "select role_id from public.user_roles "
                    "where tenant_id = :tid and user_id = :uid"
                ),
                {"tid": tenant_id, "uid": user_id},
            )
        )
        .scalars()
        .all()
    )
    return {str(item) for item in rows}


async def _principal_permission_codes(
    session: AsyncSession, tenant_id: UUID, user_id: UUID
) -> set[str]:
    """操作者当前实际持有的权限码并集（从库里算，不用 `principal.permissions`）。

    「净增」判据要拿操作者**此刻**已有什么作分母。`principal.permissions` 是请求进入时由
    `/auth/me` 那条链路解析出来的快照，而守卫跑在写事务里：同一账号在两个标签页并发操作
    （一次绑角色、一次配权限）时，快照可能已经落后一格，把「其实已经有了」误判成自扩权，
    或反过来漏掉刚被撤掉的那条。既然角色绑定已经读库了，权限并集没有理由继续吃缓存。
    """

    rows: list[Any] = list(
        (
            await session.execute(
                text(
                    "select distinct p.code from public.user_roles ur "
                    "join public.role_permissions rp on rp.role_id = ur.role_id "
                    "  and rp.tenant_id = ur.tenant_id "
                    "join public.permissions p on p.id = rp.permission_id "
                    "where ur.tenant_id = :tid and ur.user_id = :uid"
                ),
                {"tid": tenant_id, "uid": user_id},
            )
        )
        .scalars()
        .all()
    )
    return {str(code) for code in rows}


def _assert_self_grant(
    principal: Principal,
    exposure: SelfGrantExposure,
    *,
    self_grant_ack: bool,
    reason: str | None,
) -> None:
    """自扩权必须显式确认（TD-191 收口，M2-UI3 简报 §6 决策 #4）。

    超管免的是打断而不是免留痕：`requires_confirmation` 为真时调用方仍写审计，只是不校验
    `ack`/理由——否则每次正常的全量配置都要超管重复勾选，运营会被迫绕道（少一个确认框，
    多一条"先建个角色再搬权限"的野路子）。
    """

    if not exposure.requires_confirmation or SUPER_ADMIN_ROLE_CODE in principal.roles:
        return
    if not self_grant_ack:
        # 消息里的码集用顿号连接：这条文案会原样进前端的自扩权提示条，
        # `['a', 'b']` 这种 Python repr 是运维在屏幕上看到的第一个字，不能是列表字面量。
        raise RoleAdminError(
            "SELF_GRANT_CONFIRMATION_REQUIRED",
            f"本次修改会给自己新增敏感权限：{'、'.join(exposure.newly_reaching_self)}；"
            "请显式确认（self_grant_ack）并填写理由",
            422,
        )
    # 理由在这里再 strip 一次而不是信任调用方的归一化：本守卫按简报 §6 应由三条写路径共用
    # （当前仅权限全量替换接线，创建角色/绑定路径待 TD-187b/d，见 TD-194），
    # "空白理由等于没写" 属于安全判据本身，不能取决于某条路径先做没做规范化。
    if not (reason or "").strip():
        raise RoleAdminError("SELF_GRANT_REASON_REQUIRED", "自扩权必须填写理由", 422)


async def replace_role_permissions(
    principal: Principal,
    role_id: UUID,
    *,
    permission_codes: list[str],
    version: int,
    reason: str | None,
    request_id: str | None,
    self_grant_ack: bool = False,
) -> RoleSummary:
    """全量替换角色权限（版本锁 + 审计 diff + 自扩权显式确认）。

    异常:
        RoleAdminError: 404（角色不存在/跨租）、409（锁定角色或版本冲突）、422（非法权限码、
            非超管试图转授 `school.role.manage`、自扩权未确认/未填理由）。
    """

    tenant_id = _require_tenant(principal)
    requested = sorted(set(permission_codes))
    _validate_codes(requested)
    try:
        _assert_delegable(principal, requested)
    except RoleAdminError as exc:
        await _audit_denied(
            principal,
            action="school.role.permissions.denied",
            resource_id=str(role_id),
            reason=exc.code,
            request_id=request_id,
        )
        raise
    normalized_reason = reason.strip() if reason and reason.strip() else None

    async with db_session(tenant_id=tenant_id) as session:
        role = await _role_row(session, tenant_id, role_id, for_update=True)
        if role is None:
            # 不存在/跨租的 id 本身是探测信号，与 MFA 侧 USER_NOT_FOUND 同口径留痕（只记判据码，不落对方租户信息）
            await _audit_denied(
                principal,
                action="school.role.permissions.denied",
                resource_id=str(role_id),
                reason="ROLE_NOT_FOUND",
                request_id=request_id,
            )
            raise RoleAdminError("ROLE_NOT_FOUND", "角色不存在", 404)
        try:
            _assert_editable(role)
        except RoleAdminError as exc:
            await _audit_denied(
                principal,
                action="school.role.permissions.denied",
                resource_id=str(role_id),
                reason=exc.code,
                request_id=request_id,
            )
            raise
        if int(role["version"]) != version:
            latest = await _summary(session, tenant_id, role_id)
            raise RoleAdminError(
                "ROLE_VERSION_CONFLICT", "角色已被他人修改，请刷新后重试", 409, latest=latest
            )

        before = await _role_permission_codes(session, tenant_id, role_id)
        # 目录漂移探针放在任何写之前：`permissions` 里缺码时不先删旧映射再回滚。
        await _assert_codes_in_catalog(session, set(requested))
        # 自扩权判定同样放在版本 CAS 之前：被拒的请求不得把 `roles.version` 推高一格
        # （否则一次 422 会让其他操作者的下一次提交假报"已被他人修改"）。
        exposure = evaluate_self_grant(
            role_id=str(role_id),
            resulting_codes=set(requested),
            principal_role_ids=await _principal_role_ids(session, tenant_id, principal.user_id),
            principal_permissions=await _principal_permission_codes(
                session, tenant_id, principal.user_id
            ),
        )
        try:
            _assert_self_grant(
                principal, exposure, self_grant_ack=self_grant_ack, reason=normalized_reason
            )
        except RoleAdminError as exc:
            await _audit_denied(
                principal,
                action="school.role.permissions.denied",
                resource_id=str(role_id),
                reason=exc.code,
                request_id=request_id,
            )
            if exposure.requires_confirmation:
                # 打断本身也是安全事件：`*.denied` 只说明"有人被拒"，报警要盯的是
                # "谁在试图给自己加敏感码、被拒了几次"。放行时留痕、拒绝时不留痕，
                # 等于只能统计成功案例，看不出有人在反复试探。
                # 消费方注意：同一 action 在两侧的 `reason` 含义不同——成功侧是操作者填写的
                # 理由（自由文本），拒绝侧是判据码（与所有 `*.denied` 一致）；按 `result` 分流。
                await _audit_denied(
                    principal,
                    action="school.role.self_grant",
                    resource_id=str(role_id),
                    reason=exc.code,
                    request_id=request_id,
                    extra={
                        "role_code": str(role["code"]),
                        "newly_reaching_self": list(exposure.newly_reaching_self),
                        "self_grant_ack": self_grant_ack,
                    },
                )
            raise

        updated = await session.execute(
            text(
                "update public.roles set version = version + 1 "
                "where id = :rid and tenant_id = :tid and version = :version "
                "returning version"
            ),
            {"rid": role_id, "tid": tenant_id, "version": version},
        )
        if updated.scalar_one_or_none() is None:
            latest = await _summary(session, tenant_id, role_id)
            raise RoleAdminError(
                "ROLE_VERSION_CONFLICT", "角色已被他人修改，请刷新后重试", 409, latest=latest
            )

        await session.execute(
            text("delete from public.role_permissions where role_id = :rid and tenant_id = :tid"),
            {"rid": role_id, "tid": tenant_id},
        )
        if requested:
            inserted: list[Any] = list(
                (
                    await session.execute(
                        text(
                            "insert into public.role_permissions "
                            "(id, tenant_id, role_id, permission_id) "
                            "select gen_random_uuid(), :tid, :rid, p.id "
                            "from public.permissions p where p.code = any(cast(:codes as text[])) "
                            "returning permission_id"
                        ),
                        {"tid": tenant_id, "rid": role_id, "codes": requested},
                    )
                )
                .scalars()
                .all()
            )
            if len(inserted) != len(requested):
                raise RoleAdminError(
                    "PERMISSION_CATALOG_OUT_OF_SYNC",
                    "权限目录与数据库不一致，请先执行 RBAC 同步",
                    409,
                )

        after = set(requested)
        await append_audit(
            session,
            action="school.role.permissions.updated",
            resource_type="role",
            resource_id=str(role_id),
            actor_realm="tenant",
            actor_id=principal.user_id,
            tenant_id=tenant_id,
            metadata={
                "role_code": str(role["code"]),
                "added": sorted(after - before),
                "removed": sorted(before - after),
                "reason": normalized_reason,
            },
            request_id=request_id,
        )
        if exposure.requires_confirmation:
            # 独立一条动作便于报警与事后撤销：`*.updated` 是所有配置都会写的流水，
            # 混在里面等于没有信号；这里只关心"能力面流向操作者本人"的那几次。
            await append_audit(
                session,
                action="school.role.self_grant",
                resource_type="role",
                resource_id=str(role_id),
                actor_realm="tenant",
                actor_id=principal.user_id,
                tenant_id=tenant_id,
                metadata={
                    "role_code": str(role["code"]),
                    "newly_reaching_self": list(exposure.newly_reaching_self),
                    "confirmed_by": (
                        "super_admin"
                        if SUPER_ADMIN_ROLE_CODE in principal.roles
                        else "self_grant_ack"
                    ),
                    "reason": normalized_reason,
                },
                request_id=request_id,
            )
        return await _summary(session, tenant_id, role_id)


async def _default_permissions(session: AsyncSession, role_code: str) -> set[str]:
    rows: list[Any] = list(
        (
            await session.execute(
                text(
                    "select permission_code from public.rbac_builtin_role_defaults "
                    "where realm = 'tenant' and role_code = :code"
                ),
                {"code": role_code},
            )
        )
        .scalars()
        .all()
    )
    return {str(code) for code in rows}


async def _default_scopes(session: AsyncSession, role_code: str) -> set[str]:
    rows: list[Any] = list(
        (
            await session.execute(
                text(
                    "select scope_type from public.rbac_builtin_role_scope_defaults "
                    "where realm = 'tenant' and role_code = :code"
                ),
                {"code": role_code},
            )
        )
        .scalars()
        .all()
    )
    return {str(scope) for scope in rows}


async def _current_scopes(session: AsyncSession, tenant_id: UUID, role_id: UUID) -> set[str]:
    rows: list[Any] = list(
        (
            await session.execute(
                text(
                    "select distinct scope_type from public.data_scopes "
                    "where role_id = :rid and tenant_id = :tid"
                ),
                {"rid": role_id, "tid": tenant_id},
            )
        )
        .scalars()
        .all()
    )
    return {str(scope) for scope in rows}


async def _affected_users(session: AsyncSession, tenant_id: UUID, role_id: UUID) -> int:
    value = await session.scalar(
        text(
            "select count(*) from public.user_roles "
            "where role_id = :rid and tenant_id = :tid"
        ),
        {"rid": role_id, "tid": tenant_id},
    )
    return int(value or 0)


async def _preview(session: AsyncSession, tenant_id: UUID, role: dict[str, Any]) -> ResetPreview:
    role_id = UUID(str(role["id"]))
    defaults_permissions = await _default_permissions(session, str(role["code"]))
    defaults_scopes = await _default_scopes(session, str(role["code"]))
    if not defaults_permissions and not defaults_scopes:
        raise RoleAdminError(
            "ROLE_DEFAULTS_UNAVAILABLE", "该角色没有可用的出厂定义，拒绝重置", 409
        )
    current_permissions = await _role_permission_codes(session, tenant_id, role_id)
    current_scopes = await _current_scopes(session, tenant_id, role_id)
    return ResetPreview(
        version=int(role["version"]),
        permissions_added=tuple(sorted(defaults_permissions - current_permissions)),
        permissions_removed=tuple(sorted(current_permissions - defaults_permissions)),
        scopes_added=tuple(sorted(defaults_scopes - current_scopes)),
        scopes_removed=tuple(sorted(current_scopes - defaults_scopes)),
        affected_users=await _affected_users(session, tenant_id, role_id),
    )


async def preview_role_reset(principal: Principal, role_id: UUID) -> ResetPreview:
    """只读预览重置出厂的影响面（不写库、不审计）。"""

    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        role = await _role_row(session, tenant_id, role_id, for_update=False)
        if role is None:
            raise RoleAdminError("ROLE_NOT_FOUND", "角色不存在", 404)
        _assert_editable(role)
        return await _preview(session, tenant_id, role)


async def reset_role(
    principal: Principal,
    role_id: UUID,
    *,
    confirm_role_code: str,
    reason: str,
    version: int,
    request_id: str | None,
) -> ResetPreview:
    """把角色恢复出厂：撤销现有权限与角色数据范围后按定义表重种。

    本路径**不接自扩权守卫**（`replace_role_permissions` 那条），依据是出厂默认与敏感清单互不
    相交这一条不变量——已由 `tests/test_self_grant_guard_unit.py`（代码侧 `seeds.py`）与
    `tests/integration/test_rbac_custom_role_constraints.py`（库侧 `rbac_builtin_role_defaults`）
    双向钉住。哪天必须给某个可编辑角色配一个敏感码作默认，先给本函数接 `_assert_self_grant`
    再改默认，否则"重置一次自己的角色"就是一条无 ack、无专项审计的自扩权通道。

    异常:
        RoleAdminError: 404、409（锁定/版本冲突/无出厂定义）、422（角色名或理由不合格）。
    """

    tenant_id = _require_tenant(principal)
    normalized_reason = reason.strip()
    if len(normalized_reason) < 10:
        raise RoleAdminError("RESET_REASON_REQUIRED", "重置理由至少 10 个字符", 422)

    async with db_session(tenant_id=tenant_id) as session:
        role = await _role_row(session, tenant_id, role_id, for_update=True)
        if role is None:
            await _audit_denied(
                principal,
                action="school.role.reset_denied",
                resource_id=str(role_id),
                reason="ROLE_NOT_FOUND",
                request_id=request_id,
            )
            raise RoleAdminError("ROLE_NOT_FOUND", "角色不存在", 404)
        try:
            _assert_editable(role)
        except RoleAdminError as exc:
            await _audit_denied(
                principal,
                action="school.role.reset_denied",
                resource_id=str(role_id),
                reason=exc.code,
                request_id=request_id,
            )
            raise
        if str(role["code"]) != confirm_role_code.strip():
            raise RoleAdminError("ROLE_CODE_MISMATCH", "确认角色名与目标角色不一致", 422)
        if int(role["version"]) != version:
            latest = await _summary(session, tenant_id, role_id)
            raise RoleAdminError(
                "ROLE_VERSION_CONFLICT", "角色已被他人修改，请刷新后重试", 409, latest=latest
            )
        # 与全量替换同口径：先做 compare-and-set 占住版本，之后的删/种都在这把锁内。
        bumped = await session.execute(
            text(
                "update public.roles set version = version + 1 "
                "where id = :rid and tenant_id = :tid and version = :version "
                "returning version"
            ),
            {"rid": role_id, "tid": tenant_id, "version": version},
        )
        if bumped.scalar_one_or_none() is None:
            latest = await _summary(session, tenant_id, role_id)
            raise RoleAdminError(
                "ROLE_VERSION_CONFLICT", "角色已被他人修改，请刷新后重试", 409, latest=latest
            )

        preview = await _preview(session, tenant_id, role)
        # 重种前的"应有条数"来自同一张定义表：FK（rbac_builtin_role_defaults.permission_code →
        # permissions.code，ON DELETE CASCADE）目前保证不会缺码，但那是**库层不变量**；
        # 一旦改成 RESTRICT/NO ACTION，join 会静默少种几行，而 `defaults_applied=true`
        # 让后续同步也不再回填——预览承诺 N 项、实种 N-k 项是必须报错而不是少一条。
        expected_defaults = await _default_permissions(session, str(role["code"]))
        expected_scopes = await _default_scopes(session, str(role["code"]))
        await session.execute(
            text("delete from public.role_permissions where role_id = :rid and tenant_id = :tid"),
            {"rid": role_id, "tid": tenant_id},
        )
        await session.execute(
            text("delete from public.data_scopes where role_id = :rid and tenant_id = :tid"),
            {"rid": role_id, "tid": tenant_id},
        )
        reseeded: list[Any] = list(
            (
                await session.execute(
                    text(
                        "insert into public.role_permissions (id, tenant_id, role_id, permission_id) "
                        "select gen_random_uuid(), :tid, :rid, p.id "
                        "from public.rbac_builtin_role_defaults d "
                        "join public.permissions p on p.code = d.permission_code "
                        "where d.realm = 'tenant' and d.role_code = :code "
                        "returning permission_id"
                    ),
                    {"tid": tenant_id, "rid": role_id, "code": str(role["code"])},
                )
            )
            .scalars()
            .all()
        )
        if len(reseeded) != len(expected_defaults):
            raise RoleAdminError(
                "PERMISSION_CATALOG_OUT_OF_SYNC",
                "权限目录与数据库不一致，请先执行 RBAC 同步",
                409,
            )
        reseeded_scopes: list[Any] = list(
            (
                await session.execute(
                    text(
                        "insert into public.data_scopes (id, tenant_id, role_id, scope_type, scope_id) "
                        "select gen_random_uuid(), :tid, :rid, d.scope_type, null "
                        "from public.rbac_builtin_role_scope_defaults d "
                        "where d.realm = 'tenant' and d.role_code = :code "
                        "returning id"
                    ),
                    {"tid": tenant_id, "rid": role_id, "code": str(role["code"])},
                )
            )
            .scalars()
            .all()
        )
        if len(reseeded_scopes) != len(expected_scopes):
            raise RoleAdminError(
                "ROLE_DEFAULTS_UNAVAILABLE", "出厂数据范围定义与库内不一致，拒绝重置", 409
            )
        await session.execute(
            text(
                "update public.roles set defaults_applied = true "
                "where id = :rid and tenant_id = :tid"
            ),
            {"rid": role_id, "tid": tenant_id},
        )
        await append_audit(
            session,
            action="school.role.reset",
            resource_type="role",
            resource_id=str(role_id),
            actor_realm="tenant",
            actor_id=principal.user_id,
            tenant_id=tenant_id,
            metadata={
                "role_code": str(role["code"]),
                "permissions_added": list(preview.permissions_added),
                "permissions_removed": list(preview.permissions_removed),
                "scopes_added": list(preview.scopes_added),
                "scopes_removed": list(preview.scopes_removed),
                "affected_users": preview.affected_users,
                "reason": normalized_reason,
            },
            request_id=request_id,
        )
        return ResetPreview(
            version=preview.version + 1,
            permissions_added=preview.permissions_added,
            permissions_removed=preview.permissions_removed,
            scopes_added=preview.scopes_added,
            scopes_removed=preview.scopes_removed,
            affected_users=preview.affected_users,
        )


__all__ = [
    "EDITABLE_ROLE_CODES",
    "LOCKED_ROLE_REASONS",
    "NON_DELEGABLE_CODES",
    "SUPER_ADMIN_ROLE_CODE",
    "TENANT_PERMISSIONS",
    "ResetPreview",
    "RoleAdminError",
    "RoleSummary",
    "list_permissions",
    "list_roles",
    "preview_role_reset",
    "replace_role_permissions",
    "reset_role",
]
