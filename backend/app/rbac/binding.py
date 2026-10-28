"""用户↔多角色绑定（M2-UI3 TD-187d）。

口径（`docs/design-briefs/M2-UI3-custom-roles-accounts.md` §14，用户 2026-10-29 拍板）：

- 「权限账号」= 绑定了 ≥1 个角色的账号；「多权限分配」= 一个账号绑多个角色、权限取并集。
  本模块是 `user_roles` 的**第一条写入路径**，也是"配好的角色终于能授出去"的落点；
- 绑定/解绑**下一个请求即生效**，不作废在线会话：`app/auth/principal.py` 每请求都重读
  `user_roles → role_permissions → permissions`，能力来自库而不是令牌快照（与 M2-UI2 权限编辑同口径）；
- 并发控制用 `users.roles_version`（迁移 `e9b4d2f6a8c1`）：读绑定取版本 → 提交带版本 →
  同一账号的两次全量替换后者 409，不会静默丢掉一个人的改动。**不碰 `token_version`**——
  那个是"令牌作废代数"，借它做绑定 CAS 会把正常配置变成一次强制重登；
- 自绑禁令（决策 #5）：操作者不能改自己的绑定。这是"新建含敏感码的角色 → 绑给自己"这条链的
  唯一承重墙，因此**在进任何数据库读写之前**就拒绝并留痕（简报 §14.5/§14.6）；
  绑定路径**不接**自扩权守卫：改的是别人的绑定、自己的绑定又被本禁令挡住，判据没有可达分支；
- 可绑定对象（决策 D）：只允许 `teacher`/`school_admin` 目标；`deleted` 视为不存在；
  持有超管角色的目标与提交超管角色一律拒绝（超管由平台开通流程管理，避免租户内互相加权/削权）；
  空绑定集合法（"收回全部权限"是离职/停岗的正常动作）；跨租 id 与不存在同形 404；
- 写路径的拒绝（自绑/目标不合规/超管/角色不可绑/目标或角色不存在）在**独立事务**写
  `school.user.roles.denied`；版本冲突不是安全事件，只回 409 + 最新绑定快照、不落拒绝审计。
"""

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import append_audit
from app.auth.principal import Principal
from app.core.metrics import record_audit_write_failure
from app.db.session import db_session
from app.rbac.admin import SUPER_ADMIN_ROLE_CODE, role_is_bindable

logger = logging.getLogger(__name__)

#: 可被绑定的目标账号类型（决策 D 第 1 条）：「权限账号」是 B 端概念，给学生绑后台角色等于
#: 绕过 B 端账号边界。只拦目标用户，不拦角色类型——教师账号可合法地同时持有 C 端 `teacher`
#: 与 B 端 `teacher_b_end`。
BINDABLE_USER_TYPES: frozenset[str] = frozenset({"teacher", "school_admin"})


class BindingAdminError(RuntimeError):
    """用户↔角色绑定的业务错误（映射为 HTTP 状态码）。"""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int,
        *,
        latest: "UserBindings | None" = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        #: 版本冲突时回带的最新绑定快照（前端据此刷新抽屉，而不是静默覆盖）。
        self.latest = latest


@dataclass(frozen=True, slots=True)
class BoundRole:
    """账号当前绑定的一个角色。"""

    id: UUID
    code: str
    name: str
    is_system: bool
    #: 能否经绑定接口授出（TD-187d）：只有超管角色为 false。与 `RoleSummary.bindable` 同源，
    #: 让绑定页不必自己维护一份"哪些角色不能选"的名单。
    bindable: bool


@dataclass(frozen=True, slots=True)
class UserBindings:
    """一个账号的绑定快照 + 生效权限并集（GET 与 PUT 成功后返回同一形状）。"""

    user_id: UUID
    display_name: str | None
    user_type: str
    status: str
    roles_version: int
    roles: tuple[BoundRole, ...]
    permission_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BindingUpdate:
    """一次全量替换的结果：最新快照 + 角色/权限码差异。

    差异在**同一个事务**内用库值算（before 取自动手前的绑定、after 取提交的角色集），
    不是让调用方拿两个时刻的快照去猜；审计写的是同一份数据。
    """

    bindings: UserBindings
    added_role_codes: tuple[str, ...]
    removed_role_codes: tuple[str, ...]
    added_permission_codes: tuple[str, ...]
    removed_permission_codes: tuple[str, ...]


def _require_tenant(principal: Principal) -> UUID:
    if principal.realm != "tenant" or principal.tenant_id is None:
        raise BindingAdminError("REALM_MISMATCH", "角色绑定只在校方租户上下文内可用", 403)
    return principal.tenant_id


async def _audit_denied(
    principal: Principal,
    *,
    action: str,
    resource_id: str,
    reason: str,
    request_id: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    """被拒的绑定操作留痕（独立事务，best-effort）。

    服务层在写事务内 raise 时外层会整体回滚，同事务写的拒绝审计会跟着消失；而"谁在试图改
    自己的绑定 / 给超管改绑定 / 拿别人的 user id 枚举"恰是安全巡检要看的东西。
    `extra` 只放判据与内部 id，不落登录名/邮箱等明文。
    """

    tenant_id = principal.tenant_id
    if tenant_id is None:
        return
    flushed = False
    try:
        async with db_session(tenant_id=tenant_id) as session:
            await append_audit(
                session,
                action=action,
                resource_type="user",
                resource_id=resource_id,
                actor_realm="tenant",
                actor_id=principal.user_id,
                tenant_id=tenant_id,
                result="denied",
                metadata={"reason": reason, **(extra or {})},
                request_id=request_id,
            )
            flushed = True
    except Exception as exc:
        if flushed:
            record_audit_write_failure(action)
        # 与 `personnel.service._audit_denied` 同口径：只记异常类型，不写 exc_info——
        # SQLAlchemy 会把 SQL 与参数拼进异常字符串，开 traceback 等于把参数写进日志（红线）。
        logger.warning(
            "角色绑定拒绝审计写入失败：%s.%s",
            type(exc).__module__,
            type(exc).__name__,
            extra={"action": action},
        )
        return


async def _user_row(
    session: AsyncSession, tenant_id: UUID, user_id: UUID, *, for_update: bool
) -> dict[str, Any] | None:
    """读目标账号（`FOR UPDATE` 同时承担"同一账号的绑定替换串行化"）。

    不用 `auth.sessions.acquire_user_lock`（advisory lock）：那把锁的语义是"会话写操作串行化"
    （登录/刷新/撤销），绑定不碰 refresh token，借它会把正常配置与登录挤成一条队列；
    行锁只和同账号的另一次绑定、以及 MFA 重置（它也锁用户行）互斥，范围正好。
    """

    sql = (
        "select id, display_name, user_type, status, roles_version "
        "from public.users where id = :uid and tenant_id = :tid for update"
        if for_update
        else "select id, display_name, user_type, status, roles_version "
        "from public.users where id = :uid and tenant_id = :tid"
    )
    row = (
        await session.execute(text(sql), {"uid": user_id, "tid": tenant_id})
    ).mappings().one_or_none()
    return None if row is None else dict(row)


def _to_bound_role(row: dict[str, Any]) -> BoundRole:
    code = str(row["code"])
    return BoundRole(
        id=UUID(str(row["id"])),
        code=code,
        name=str(row["name"]),
        is_system=bool(row["is_system"]),
        bindable=role_is_bindable(code),
    )


async def _bound_roles(
    session: AsyncSession, tenant_id: UUID, user_id: UUID
) -> list[BoundRole]:
    rows = (
        (
            await session.execute(
                text(
                    "select r.id, r.code, r.name, r.is_system "
                    "from public.user_roles ur "
                    "join public.roles r on r.id = ur.role_id and r.tenant_id = ur.tenant_id "
                    "where ur.user_id = :uid and ur.tenant_id = :tid "
                    "order by r.code"
                ),
                {"uid": user_id, "tid": tenant_id},
            )
        )
        .mappings()
        .all()
    )
    return [_to_bound_role(dict(row)) for row in rows]


async def _roles_by_ids(
    session: AsyncSession, tenant_id: UUID, role_ids: list[UUID]
) -> list[BoundRole]:
    """按 id 取角色并持 `FOR KEY SHARE`（跨租与不存在同形返回缺失）。

    锁是给后面的 `user_roles` 插入用的：插入时外键会对引用的角色行取同档的 `KEY SHARE`，
    这里提前拿住，让"校验通过后角色被并发删除"只有两种结局——删除先完成（这里读到 0 行 →
    404 + 拒绝审计），或删除的 `FOR UPDATE` 被挡到本事务提交之后（删除侧由影响面指纹兜住，
    见 `delete_role`）。没有这把锁时，第二种时序会在插入处撞外键，把一次正常的并发暴露成 500。
    """

    if not role_ids:
        return []
    rows = (
        (
            await session.execute(
                text(
                    "select id, code, name, is_system from public.roles "
                    "where tenant_id = :tid and id = any(cast(:rids as uuid[])) "
                    "for key share"
                ),
                {"tid": tenant_id, "rids": [str(rid) for rid in role_ids]},
            )
        )
        .mappings()
        .all()
    )
    return [_to_bound_role(dict(row)) for row in rows]


async def _permission_codes_for_roles(
    session: AsyncSession, tenant_id: UUID, role_ids: list[UUID]
) -> set[str]:
    """若干角色的权限并集（与 `principal.py` 的解析同形：`user_roles → role_permissions → permissions`）。"""

    if not role_ids:
        return set()
    rows: list[Any] = list(
        (
            await session.execute(
                text(
                    "select distinct p.code from public.role_permissions rp "
                    "join public.permissions p on p.id = rp.permission_id "
                    "where rp.tenant_id = :tid and rp.role_id = any(cast(:rids as uuid[]))"
                ),
                {"tid": tenant_id, "rids": [str(rid) for rid in role_ids]},
            )
        )
        .scalars()
        .all()
    )
    return {str(code) for code in rows}


async def _bindings(
    session: AsyncSession, tenant_id: UUID, user: dict[str, Any]
) -> UserBindings:
    user_id = UUID(str(user["id"]))
    roles = await _bound_roles(session, tenant_id, user_id)
    permission_codes = await _permission_codes_for_roles(
        session, tenant_id, [role.id for role in roles]
    )
    return UserBindings(
        user_id=user_id,
        display_name=None if user["display_name"] is None else str(user["display_name"]),
        user_type=str(user["user_type"]),
        status=str(user["status"]),
        roles_version=int(user["roles_version"]),
        roles=tuple(roles),
        permission_codes=tuple(sorted(permission_codes)),
    )


async def get_user_bindings(principal: Principal, user_id: UUID) -> UserBindings:
    """该账号当前的绑定与生效权限并集（只读，不审计成功查询）。

    异常:
        BindingAdminError: 403（主体 realm 不符）、404（不存在/跨租/已删除）。
    """

    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        user = await _user_row(session, tenant_id, user_id, for_update=False)
        if user is None or str(user["status"]) == "deleted":
            raise BindingAdminError("USER_NOT_FOUND", "用户不存在", 404)
        return await _bindings(session, tenant_id, user)


async def replace_user_bindings(
    principal: Principal,
    user_id: UUID,
    *,
    role_ids: list[UUID],
    version: int,
    reason: str | None,
    request_id: str | None,
) -> BindingUpdate:
    """全量替换一个账号的角色绑定集（`users.roles_version` CAS + 差异审计）。

    判据顺序（越权先于并发错误，输入有效性先于 CAS）：
    自绑 → 目标存在 → 目标超管 → 目标类型 → 提交角色存在 → 角色可绑 → 绑定版本 CAS。
    后两步里"提交角色存在"是输入合法性的最后一环，放在 CAS 前：角色被删了就该报 404 让调用方
    刷新列表，用 409 盖住它只会让人以为并发一下就能成功。

    异常:
        BindingAdminError: 403（realm 不符/自绑/目标超管）、404（目标或角色不存在/跨租）、
            409（`BINDING_VERSION_CONFLICT`，回带最新快照）、422（目标类型不可绑/超管角色）。
    """

    tenant_id = _require_tenant(principal)
    normalized_reason = reason.strip() if reason and reason.strip() else None

    # 自绑禁令在任何数据库读写之前：这条判据不依赖目标是否存在，越早拒绝越不可能被"顺手"
    # 加进某个写分支后面。它是"新建含敏感码的角色 → 绑给自己"这条链的唯一承重墙（简报 §14.5）。
    if principal.user_id == user_id:
        await _audit_denied(
            principal,
            action="school.user.roles.denied",
            resource_id=str(user_id),
            reason="BINDING_SELF_FORBIDDEN",
            request_id=request_id,
        )
        raise BindingAdminError(
            "BINDING_SELF_FORBIDDEN", "不能修改自己的角色绑定；请由其他管理员操作", 403
        )

    # 去重保序：重复 id 等价于提交一次；顺序不影响结果（差异按码集算），保持首次出现顺序便于阅读。
    submitted_ids = list(dict.fromkeys(role_ids))

    async with db_session(tenant_id=tenant_id) as session:
        user = await _user_row(session, tenant_id, user_id, for_update=True)
        if user is None or str(user["status"]) == "deleted":
            await _audit_denied(
                principal,
                action="school.user.roles.denied",
                resource_id=str(user_id),
                reason="USER_NOT_FOUND",
                request_id=request_id,
            )
            raise BindingAdminError("USER_NOT_FOUND", "用户不存在", 404)

        before_roles = await _bound_roles(session, tenant_id, user_id)
        if any(role.code == SUPER_ADMIN_ROLE_CODE for role in before_roles):
            # 增、删、清空都不行：超管由平台开通流程管理，与 MFA 重置同一保护口径。
            await _audit_denied(
                principal,
                action="school.user.roles.denied",
                resource_id=str(user_id),
                reason="BINDING_SUPER_ADMIN_FORBIDDEN",
                request_id=request_id,
            )
            raise BindingAdminError(
                "BINDING_SUPER_ADMIN_FORBIDDEN",
                "校方超管的角色绑定由平台开通流程管理，租户内不可修改",
                403,
            )
        user_type = str(user["user_type"])
        if user_type not in BINDABLE_USER_TYPES:
            await _audit_denied(
                principal,
                action="school.user.roles.denied",
                resource_id=str(user_id),
                reason="BINDING_TARGET_NOT_ALLOWED",
                request_id=request_id,
                extra={"target_user_type": user_type},
            )
            raise BindingAdminError(
                "BINDING_TARGET_NOT_ALLOWED", "只能为教师或校方管理员账号绑定角色", 422
            )

        submitted_roles = await _roles_by_ids(session, tenant_id, submitted_ids)
        if len(submitted_roles) != len(submitted_ids):
            # 跨租与不存在的 id 同形 404，不泄露存在性；被探测的 id 进 metadata——
            # 只看目标 user_id 回答不了"谁在拿别人家的 role id 枚举"。
            await _audit_denied(
                principal,
                action="school.user.roles.denied",
                resource_id=str(user_id),
                reason="ROLE_NOT_FOUND",
                request_id=request_id,
                extra={"submitted_role_ids": [str(rid) for rid in submitted_ids]},
            )
            raise BindingAdminError("ROLE_NOT_FOUND", "角色不存在", 404)
        if any(role.code == SUPER_ADMIN_ROLE_CODE for role in submitted_roles):
            await _audit_denied(
                principal,
                action="school.user.roles.denied",
                resource_id=str(user_id),
                reason="ROLE_NOT_BINDABLE",
                request_id=request_id,
            )
            raise BindingAdminError(
                "ROLE_NOT_BINDABLE", "校方超管角色不能经绑定接口授予", 422
            )

        if int(user["roles_version"]) != version:
            # 并发改绑不是安全事件：不回拒绝审计，只带最新快照让调用方刷新（与角色版本冲突同口径）。
            latest = await _bindings(session, tenant_id, user)
            raise BindingAdminError(
                "BINDING_VERSION_CONFLICT", "该账号的角色绑定已被他人修改，请刷新后重试", 409,
                latest=latest,
            )

        before_codes = {role.code for role in before_roles}
        before_permissions = await _permission_codes_for_roles(
            session, tenant_id, [role.id for role in before_roles]
        )
        after_permissions = await _permission_codes_for_roles(
            session, tenant_id, submitted_ids
        )
        after_codes = {role.code for role in submitted_roles}
        added_roles = tuple(sorted(after_codes - before_codes))
        removed_roles = tuple(sorted(before_codes - after_codes))
        added_permissions = tuple(sorted(after_permissions - before_permissions))
        removed_permissions = tuple(sorted(before_permissions - after_permissions))

        await session.execute(
            text("delete from public.user_roles where user_id = :uid and tenant_id = :tid"),
            {"uid": user_id, "tid": tenant_id},
        )
        if submitted_ids:
            await session.execute(
                text(
                    "insert into public.user_roles (tenant_id, user_id, role_id) "
                    "select :tid, :uid, r.id from unnest(cast(:rids as uuid[])) as r(id)"
                ),
                {"tid": tenant_id, "uid": user_id, "rids": [str(rid) for rid in submitted_ids]},
            )
        bumped = await session.execute(
            text(
                "update public.users set roles_version = roles_version + 1, updated_at = now() "
                "where id = :uid and tenant_id = :tid returning roles_version"
            ),
            {"uid": user_id, "tid": tenant_id},
        )
        new_version = int(bumped.scalar_one())

        await append_audit(
            session,
            action="school.user.roles.updated",
            resource_type="user",
            resource_id=str(user_id),
            actor_realm="tenant",
            actor_id=principal.user_id,
            tenant_id=tenant_id,
            metadata={
                "role_codes_before": sorted(before_codes),
                "role_codes_after": sorted(after_codes),
                "added_role_codes": list(added_roles),
                "removed_role_codes": list(removed_roles),
                "added_permission_codes": list(added_permissions),
                "removed_permission_codes": list(removed_permissions),
                "reason": normalized_reason,
            },
            request_id=request_id,
        )

    after = UserBindings(
        user_id=user_id,
        display_name=None if user["display_name"] is None else str(user["display_name"]),
        user_type=user_type,
        status=str(user["status"]),
        roles_version=new_version,
        roles=tuple(sorted(submitted_roles, key=lambda role: role.code)),
        permission_codes=tuple(sorted(after_permissions)),
    )
    return BindingUpdate(
        bindings=after,
        added_role_codes=added_roles,
        removed_role_codes=removed_roles,
        added_permission_codes=added_permissions,
        removed_permission_codes=removed_permissions,
    )


__all__ = [
    "BINDABLE_USER_TYPES",
    "BindingAdminError",
    "BindingUpdate",
    "BoundRole",
    "UserBindings",
    "get_user_bindings",
    "replace_user_bindings",
]
