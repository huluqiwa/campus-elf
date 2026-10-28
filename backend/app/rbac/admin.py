"""角色权限配置（TD-137/TD-143）+ 自定义角色增删（TD-187b）+ 角色级数据范围编辑（TD-187c）。

口径（`docs/design-briefs/M2-UI2-rbac-admin.md` §3/§4 与 `M2-UI3-custom-roles-accounts.md` §4/§13）：

- 可编辑对象 = 两个内置可编辑角色（`school_admin`、`teacher_b_end`）+ 全部自定义角色
  （`is_system=false`，M2-UI3 开放）；超管与 C 端基础角色锁定，避免把登录/对话/审批入口改坏或自锁；
- 映射是**运营配置**：接口全量替换并写审计 diff；`roles.version` 乐观锁拒绝并发覆盖；
- 重置出厂恢复**权限与数据范围**两侧，执行前可预览影响面；自定义角色无出厂定义 → 重置 409
  `ROLE_DEFAULTS_UNAVAILABLE`（从 M2-UI2 的"待 M2-UI3"改为明确语义）；
- 自定义角色可创建（空集或复制自同租户可编辑角色）与硬删（预览影响面 + 逐字确认 + 理由 + 版本 CAS，
  删除前把完整权限码集快照进审计）；内置角色不可删（409 `ROLE_NOT_DELETABLE`）；
  删除契约同时带 `version` 与**影响面指纹**（TD-199）：前者挡"预览后角色权限/范围被改"，
  后者挡"预览后持有人集合被改"（绑定不推进 `roles.version`），两套判据各管一件事；
- `school.role.manage` 只能由校方超管本人授出（转授即等于无条件把本校全部权限交出去）；
- 自扩权（TD-191 收口）：结果集触达操作者自己所在角色、且净增敏感码时，必须带
  `self_grant_ack=true` + 理由（超管免打断）。`school.role.self_grant` 审计**放行与打断都写**
  ——放行时同事务，被打断时走独立事务（外层回滚会带走同事务的留痕）。
  TD-194 起判据/打断/两侧留痕收在 `_guard_self_grant` + `_audit_self_grant_applied` 一处，
  三个入口共用：角色权限替换、角色范围替换、**重置出厂（权限 + 范围两个维度）**；
  用户↔角色绑定**不接**（改的是别人的绑定，且自绑被禁，判据无可达分支，简报 §14.5）；
- 角色级数据范围（TD-187c）：`school`/`self` 可全量替换，与权限替换**共享同一列 `roles.version`**
  （一列版本覆盖两条写路径，避免出现"权限已更新、范围更新失败"的半更新态，代价见 TD-202）；
  `class`/`course` **不可手工授**且被拒要留痕——这两类必须绑定到具体班级/课程 id（库 CHECK
  `ck_data_scopes_scope_id_required_for_class_course`），而角色配置这条路径只有"类型"这一维入参、
  没有"选哪个班"的通道，写下去就是缺 id 的脏数据（该角色每个持有人下一次请求在范围解析处 500）；
  `course` 另外还本期不贡献可见性（TD-151）。用户级班级行才归 `app/org/scopes_sync.py` 的差量同步
  所有——它只读/删 `user_id is not null` 的行，角色级同型行今天**没有任何合法写入方**，
  本端点既不 owns 也不碰（删除语句限定在可编辑那两类）。
  把本人所在角色从 `self` 抬成 `school` 与补一个敏感码是同一件事，走同一条自扩权确认；
  空范围清单**合法**（解析层 fail-closed 成"看不到数据"，不会放大可见性）；
- 所有写入在租户连接内完成（RLS + 显式 tenant_id 过滤），跨租 role_id 与不存在同形 404；
- 被拒的写操作（锁定角色、非法转授）在**独立事务**写 `*.denied` 审计——外层事务会回滚，
  同事务写的拒绝留痕跟着一起消失。
"""

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import append_audit
from app.auth.principal import Principal
from app.core.metrics import record_audit_write_failure
from app.db.session import db_session
from app.rbac.catalog import PERMISSION_CATALOG, TENANT_MODULES, PermissionDef, permission_def
from app.rbac.scopes import KNOWN_SCOPE_TYPES
from app.rbac.seeds import builtin_roles
from app.rbac.self_grant import SelfGrantExposure, evaluate_scope_self_grant, evaluate_self_grant

logger = logging.getLogger(__name__)

# 内置可编辑角色：入库即有、可运营配置；其余内置角色锁定。
EDITABLE_ROLE_CODES: frozenset[str] = frozenset({"school_admin", "teacher_b_end"})
# 转授禁令（用户 2026-10-26 决策，见 `_assert_delegable`）。
NON_DELEGABLE_CODES: frozenset[str] = frozenset({"school.role.manage"})
SUPER_ADMIN_ROLE_CODE = "school_super_admin"
LOCKED_ROLE_REASONS: dict[str, str] = {
    "school_super_admin": "校方超管固定为本校 school+campus 全量权限，防止自锁；调整走平台流程",
    "student": "C 端学生基础角色锁定，避免改坏登录/对话/请假入口",
    "teacher": "C 端教师基础角色锁定，避免改坏登录/对话/请假入口",
}
# 内置角色码保留名单（M2-UI3 TD-187b）：自定义角色不得占用这些码。库层
# `ck_roles_builtin_code_is_system` 已挡（非 is_system 行撞内置码即违反约束 + `uq_roles_tenant_id_code`
# 同码唯一），这里在接口层提前给出人读 409 而不是让 INSERT 撞库。与 `seeds.py` 同源，不写字面量。
RESERVED_TENANT_ROLE_CODES: frozenset[str] = frozenset(
    str(role.code) for role in builtin_roles("tenant")
)
# 自定义角色 code 格式（与迁移 `d7a1c3e5b9f4` 的 `ck_roles_code_format_valid` 逐字符同界）。
# 长度上限 63 而非列宽 64：见简报 §4.1——按列宽写校验会让 64 字符穿过接口、在 INSERT 撞 CHECK 变 500。
ROLE_CODE_PATTERN = r"^[a-z][a-z0-9_]{2,62}$"
# 并发创建兜底用的唯一约束名（`779f731db342` 建表时定义）：只有它翻成人读 409，其余完整性错误
# 保持原样抛出，避免把真故障伪装成业务冲突。
_ROLE_CODE_UNIQUE_CONSTRAINT = "uq_roles_tenant_id_code"
# 角色级数据范围的可编辑子集（M2-UI3 TD-187c，简报 §5/§13）：只有这两类是"配角色"的语义。
# `class`/`course` 必须绑定到具体班级/课程 id（库 CHECK `ck_data_scopes_scope_id_required_for_class_course`），
# 而本端点的入参只有"类型"这一维、没有"选哪个班"的通道——写下去就是缺 id 的脏数据；`course`
# 另外还本期不贡献可见性（TD-151）。两者一律拒授并留痕，不是"尚未开放"。
EDITABLE_SCOPE_TYPES: frozenset[str] = frozenset({"school", "self"})
# 不可编辑范围类型的人读理由（错误消息里直接引用，避免文案与判据分家）。
_SCOPE_TYPE_OWNERSHIP: dict[str, str] = {
    "class": "班级范围必须绑定到具体班级 id（库 CHECK 强制），角色配置这条路径没有"
    "「选哪个班」的入参通道，写下去是缺 id 的脏数据；用户级班级行由「任教 ∪ 班主任 ∪ 辅导员」"
    "关系自动同步（app/org/scopes_sync.py），角色级同型行今天没有合法写入方",
    "course": "课程范围本期不贡献可见性（TD-151），配置了也不生效",
}
# 范围内插漂移兜底用的 CHECK 约束名（迁移 `779f731db342` 建表时定义）：只有它翻成人读 409，
# 其余完整性错误保持原样抛出，避免把真故障伪装成业务冲突。
# 写全名而不是 `scope_type_valid`：`app/db/base.py` 的 NAMING_CONVENTION 给约束名加了
# `ck_<table>_` 前缀，异步驱动报的是前缀后的完整名字（比对过 `pg_constraint.conname`）。
_SCOPE_TYPE_CHECK_CONSTRAINT = "ck_data_scopes_scope_type_valid"
_SYSTEM_ROLE_REASON = "该内置角色不在可编辑范围"

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
        latest_preview: "RoleDeletionPreview | None" = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.latest = latest
        # 删除预览过期时回带的**最新影响面**（人数 + 指纹 + 版本）：调用方据此刷新确认框，
        # 不必先跳一次 GET 再回来重试（简报 §14.3）。
        self.latest_preview = latest_preview


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
    #: 能否经绑定接口授给账号（TD-187d）。只有超管角色为 false——它由平台开通流程管理，
    #: 允许租户内绑定等于让持 `permission_account.manage` 的人把任意账号提成超管。
    bindable: bool


@dataclass(frozen=True, slots=True)
class ResetPreview:
    """重置出厂的影响面（预览与执行返回同一形状）。"""

    version: int
    permissions_added: tuple[str, ...]
    permissions_removed: tuple[str, ...]
    scopes_added: tuple[str, ...]
    scopes_removed: tuple[str, ...]
    affected_users: int


@dataclass(frozen=True, slots=True)
class RoleDeletionPreview:
    """删除自定义角色的影响面（预览与执行返回同一形状）。

    `permission_codes` 是**删除前**的完整码集快照——CASCADE 之后 `role_permissions` 行即消失，
    只能靠这份快照复盘"被删的角色当时能干什么"（简报 §4.2：删除留完整码集审计是硬要求，不是可选）。

    `fingerprint` 是**持有人集合**的哈希（TD-199）：绑定不推进 `roles.version`，预览→确认之间
    多绑或少绑一个人不会被版本 CAS 挡下，由指纹负责。只回哈希、不回显持有人清单（简报 §14.3）。
    """

    role_code: str
    name: str
    version: int
    affected_users: int
    permission_codes: tuple[str, ...]
    fingerprint: str


def _require_tenant(principal: Principal) -> UUID:
    """角色配置只接受租户主体。"""

    if principal.realm != "tenant" or principal.tenant_id is None:
        raise RoleAdminError("REALM_MISMATCH", "角色配置只在校方租户上下文内可用", 403)
    return principal.tenant_id


def _lock_reason(code: str, *, is_system: bool) -> str | None:
    """不可编辑角色给出人读原因；可编辑返回 None。

    M2-UI3 TD-187b：自定义角色（`is_system=false`）从「待开放」转为**完全可编辑**——新建角色
    若不能配权限就是死配置。内置角色仍按 `EDITABLE_ROLE_CODES`/`LOCKED_ROLE_REASONS` 划分。
    """

    if not is_system:
        return None
    if code in EDITABLE_ROLE_CODES:
        return None
    if code in LOCKED_ROLE_REASONS:
        return LOCKED_ROLE_REASONS[code]
    return _SYSTEM_ROLE_REASON


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


def role_is_bindable(code: str) -> bool:
    """该角色能否经绑定接口授出（TD-187d）：只有超管角色为 false。

    `RoleSummary.bindable` 与 `BoundRole.bindable` 共用这一处判据——两边各写一次
    `code != SUPER_ADMIN_ROLE_CODE` 迟早会分叉（例如将来"平台角色"也进禁授名单时只改一边）。
    """

    return code != SUPER_ADMIN_ROLE_CODE


def _to_summary(row: Any) -> RoleSummary:
    is_system = bool(row["is_system"])
    code = str(row["code"])
    return RoleSummary(
        id=UUID(str(row["id"])),
        code=code,
        name=str(row["name"]),
        is_system=is_system,
        editable=(not is_system) or code in EDITABLE_ROLE_CODES,
        lock_reason=_lock_reason(code, is_system=is_system),
        version=int(row["version"]),
        defaults_applied=bool(row["defaults_applied"]),
        user_count=int(row["user_count"]),
        permission_codes=tuple(str(code) for code in row["permission_codes"]),
        bindable=role_is_bindable(code),
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

    `extra` 用于判据本身需要上下文才可读的动作（自扩权要看到具体敏感码清单、复制源不存在要看到
    被探测的 role id）；只放判定用的码/布尔与内部 id，PII 与角色名一律不进审计。
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
                resource_type="role",
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
            # append_audit 已成功 flush，异常只能来自事务提交；它记不到这里，补一次计数。
            record_audit_write_failure(action)
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


async def _principal_scope_types(
    session: AsyncSession, tenant_id: UUID, user_id: UUID
) -> set[str]:
    """操作者此刻实际生效的数据范围类型并集（角色级 ∪ 用户级），判据的"净增"分母。

    谓词与 `app/auth/principal.py:DATA_SCOPE_SELECT_SQL` 同形（角色范围与用户范围取并集，
    矩阵 §2「用户级授权是额外给，不是收窄」）——两处不一致时守卫会按一个运行时并不存在的
    能力面来判"扩没扩权"。与权限侧同一理由，读库而不吃 `principal.scope_types` 的请求快照。
    """

    rows: list[Any] = list(
        (
            await session.execute(
                text(
                    "select distinct s.scope_type from public.data_scopes s "
                    "where s.tenant_id = :tid "
                    "and (s.user_id = :uid or exists ( "
                    "  select 1 from public.user_roles ur "
                    "  where ur.tenant_id = :tid and ur.user_id = :uid and ur.role_id = s.role_id"
                    "))"
                ),
                {"tid": tenant_id, "uid": user_id},
            )
        )
        .scalars()
        .all()
    )
    return {str(scope) for scope in rows}


def _assert_self_grant(
    principal: Principal,
    exposure: SelfGrantExposure,
    *,
    self_grant_ack: bool,
    reason: str | None,
    kind: Literal["permission", "scope"] = "permission",
) -> None:
    """自扩权必须显式确认（TD-191 收口，M2-UI3 简报 §6 决策 #4 与 §13.2 决策①）。

    超管免的是打断而不是免留痕：`requires_confirmation` 为真时调用方仍写审计，只是不校验
    `ack`/理由——否则每次正常的全量配置都要超管重复勾选，运营会被迫绕道（少一个确认框，
    多一条"先建个角色再搬权限"的野路子）。

    `kind` 只影响文案（权限码 vs 数据范围），判据结构是同一个：两条腿共用这套
    ack/理由/超管豁免的规则，比复制一份"范围版打断"更难走偏（TD-194 抽 helper 时
    `kind` 就是这个入参，不要拆成两个函数）。
    """

    if not exposure.requires_confirmation or SUPER_ADMIN_ROLE_CODE in principal.roles:
        return
    if not self_grant_ack:
        # 消息里的码集用顿号连接：这条文案会原样进前端的自扩权提示条，
        # `['a', 'b']` 这种 Python repr 是运维在屏幕上看到的第一个字，不能是列表字面量。
        items = "、".join(exposure.newly_reaching_self)
        detail = (
            f"本次修改会给自己新增敏感权限：{items}"
            if kind == "permission"
            else f"本次修改会扩大自己可见的数据范围：{items}（`school` 即全校）"
        )
        raise RoleAdminError(
            "SELF_GRANT_CONFIRMATION_REQUIRED",
            f"{detail}；请显式确认（self_grant_ack）并填写理由",
            422,
        )
    # 理由在这里再 strip 一次而不是信任调用方的归一化：本判据由 `_guard_self_grant` 统一调用，
    # 覆盖角色权限替换、角色范围替换、重置出厂（两个维度）四个入口；创建自定义角色与用户绑定
    # **不接**（前者新角色 id 必不在操作者所在角色集合里；后者改的是别人的绑定且自绑被禁，
    # 判据均无可达分支——2026-10-27/29 用户裁定，见简报 §12、§14.5 与 TD-194），
    # "空白理由等于没写" 属于安全判据本身，不能取决于某条路径先做没做规范化。
    if not (reason or "").strip():
        raise RoleAdminError("SELF_GRANT_REASON_REQUIRED", "自扩权必须填写理由", 422)


async def _guard_self_grant(
    principal: Principal,
    *,
    role: dict[str, Any],
    exposures: list[tuple[Literal["permission", "scope"], SelfGrantExposure]],
    self_grant_ack: bool,
    reason: str | None,
    denied_action: str,
    request_id: str | None,
) -> list[tuple[Literal["permission", "scope"], SelfGrantExposure]]:
    """自扩权守卫的**唯一入口**（TD-194）：校验 → 打断并双侧留痕 → 返回命中项。

    `exposures` 是 `(kind, 判定)` 列表：权限替换传一项、范围替换传一项、重置出厂传两项
    （重置同时重种权限与范围，两个维度都要过；任一未确认即 422）。命中的维度由调用方在写事务
    提交前交给 `_audit_self_grant_applied` 记成功侧留痕——那一条必须和写操作同一个事务。

    被打断时写两类拒绝审计：调用方自己的 `*.denied`（说明"哪条写路径被拒"）与每个命中维度
    一条 `school.role.self_grant`（说明"哪几个敏感能力差点流向本人"）。两者都走独立事务，
    外层回滚带不走它们；审计故障不改变拒绝结果。
    """

    hits = [item for item in exposures if item[1].requires_confirmation]
    if not hits:
        return []
    if SUPER_ADMIN_ROLE_CODE not in principal.roles:
        try:
            for kind, exposure in hits:
                _assert_self_grant(
                    principal,
                    exposure,
                    self_grant_ack=self_grant_ack,
                    reason=reason,
                    kind=kind,
                )
        except RoleAdminError as exc:
            await _audit_denied(
                principal,
                action=denied_action,
                resource_id=str(role["id"]),
                reason=exc.code,
                request_id=request_id,
            )
            for kind, exposure in hits:
                await _audit_denied(
                    principal,
                    action="school.role.self_grant",
                    resource_id=str(role["id"]),
                    reason=exc.code,
                    request_id=request_id,
                    extra={
                        "role_code": str(role["code"]),
                        "kind": kind,
                        "newly_reaching_self": list(exposure.newly_reaching_self),
                        "self_grant_ack": self_grant_ack,
                    },
                )
            raise
    return hits


async def _audit_self_grant_applied(
    session: AsyncSession,
    principal: Principal,
    *,
    tenant_id: UUID,
    role: dict[str, Any],
    hits: list[tuple[Literal["permission", "scope"], SelfGrantExposure]],
    reason: str | None,
    request_id: str | None,
) -> None:
    """放行侧留痕：每个命中维度一条 `school.role.self_grant`（与写操作同事务）。

    独立动作便于报警与事后撤销：`*.updated`/`school.role.reset` 是所有配置都会写的流水，
    混在里面等于没有信号；这里只关心"能力面流向操作者本人"的那几次。
    消费方注意：同一 action 在放行/打断两侧的 `reason` 含义不同（自由文本 vs 判据码），按 `result` 分流。
    """

    for kind, exposure in hits:
        await append_audit(
            session,
            action="school.role.self_grant",
            resource_type="role",
            resource_id=str(role["id"]),
            actor_realm="tenant",
            actor_id=principal.user_id,
            tenant_id=tenant_id,
            metadata={
                "role_code": str(role["code"]),
                "kind": kind,
                "newly_reaching_self": list(exposure.newly_reaching_self),
                "confirmed_by": (
                    "super_admin"
                    if SUPER_ADMIN_ROLE_CODE in principal.roles
                    else "self_grant_ack"
                ),
                "reason": reason,
            },
            request_id=request_id,
        )


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
        hits = await _guard_self_grant(
            principal,
            role=role,
            exposures=[("permission", exposure)],
            self_grant_ack=self_grant_ack,
            reason=normalized_reason,
            denied_action="school.role.permissions.denied",
            request_id=request_id,
        )

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
            await _audit_self_grant_applied(
                session,
                principal,
                tenant_id=tenant_id,
                role=role,
                hits=hits,
                reason=normalized_reason,
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


async def _affected_user_ids(
    session: AsyncSession, tenant_id: UUID, role_id: UUID, *, for_update: bool = False
) -> list[UUID]:
    """角色当前持有人（按 id 排序，指纹的输入）。

    `for_update=True` 只给删除执行路径用：锁住持有人行，让「重算指纹 → CASCADE 删除」之间
    不再有解绑落库。解绑是对 `user_roles` 行做 DELETE，会被这把行锁挡住；不锁时并发解绑
    可以在这段窗口里提交，删除响应与审计的 `affected_users` 就会多算一个已经解绑的人。
    预览不锁：它本来就是快照，把一次只读点击变成持有全部持有人行锁没有必要。

    锁序仍是 `roles → user_roles`（CASCADE 自己也是这个顺序），绑定路径是
    `users → roles → user_roles`，不存在环。
    """

    sql = (
        "select user_id from public.user_roles "
        "where role_id = :rid and tenant_id = :tid order by user_id"
        + (" for update" if for_update else "")
    )
    rows: list[Any] = list(
        (await session.execute(text(sql), {"rid": role_id, "tid": tenant_id}))
        .scalars()
        .all()
    )
    return [UUID(str(item)) for item in rows]


def _deletion_fingerprint(role_id: UUID, user_ids: list[UUID]) -> str:
    """删除影响面指纹 = SHA-256(role_id ∪ 排序后的持有人 id) 的十六进制（TD-199，简报 §14.3）。

    只覆盖**持有人集合**，不覆盖权限码与人数以外的任何状态：权限/范围在预览后被改由
    `roles.version` 挡，两套判据各管一件事，避免同一次变化触发两条错误码。
    集合语义：`[a, b]` 与 `[b, a]` 同哈希；空集合仍带 `role_id`，所以"把 A 的所有持有人搬给 B"
    不会让两个角色的指纹撞成同一个。不做过期时间——它绑定的是状态而不是时间。
    """

    material = f"{role_id}|{','.join(sorted(str(item) for item in user_ids))}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


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
    self_grant_ack: bool = False,
) -> ResetPreview:
    """把角色恢复出厂：撤销现有权限与角色数据范围后按定义表重种。

    自 TD-194 起**接自扩权守卫，且权限与范围两个维度都过**（`_guard_self_grant`）：
    - 权限侧：出厂默认与 `SENSITIVE_SELF_GRANT_CODES` 互不相交，这条不变量仍由
      `tests/test_self_grant_guard_unit.py`（代码侧）与
      `tests/integration/test_rbac_custom_role_constraints.py`（库侧）双向钉住，判据通常不触发；
    - 范围侧是**可达路径**：`SENSITIVE_SCOPE_TYPES = {school}`，而 `school_admin` 的出厂范围
      就含 `school`。同租的另一名管理员把 `school_admin` 收窄成 `self` → 该角色的持有人点一次
      「恢复出厂」→ 范围净增回 `school`、拿回全校可见。没有 ack 的这次重置现在 422
      `SELF_GRANT_CONFIRMATION_REQUIRED`，带 ack 则放行并写 `school.role.self_grant`。
    超管主体免打断不免留痕（与另外两个入口同口径）。
    哪天必须给某个可编辑角色配一个敏感码作默认，先接守卫再改默认（这条路径已接，无需再改）。

    异常:
        RoleAdminError: 404、409（锁定/版本冲突/无出厂定义）、
            422（角色名或理由不合格、自扩权未确认/未填理由）。
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
        # 出厂定义检查放在版本 CAS **之前**：自定义角色没有出厂定义，重置对它是结构性不可能。
        # 放在后面也能靠事务回滚抹掉版本增量，但"这单根本不该进写路径"的判断留在顺序里比留在
        # 回滚里好读，也不用在每次拒绝时去抢行锁、推一次版本再撤销（并发同事会看到无谓的 409）。
        preview = await _preview(session, tenant_id, role)
        # 重置的结果集 = 定义表里的出厂权限与范围，两个维度都要过守卫（TD-194）。
        # 判定同样排在版本 CAS 之前：被拒的重置不得推高 `roles.version`。
        principal_role_ids = await _principal_role_ids(session, tenant_id, principal.user_id)
        permission_exposure = evaluate_self_grant(
            role_id=str(role_id),
            resulting_codes=await _default_permissions(session, str(role["code"])),
            principal_role_ids=principal_role_ids,
            principal_permissions=await _principal_permission_codes(
                session, tenant_id, principal.user_id
            ),
        )
        scope_exposure = evaluate_scope_self_grant(
            role_id=str(role_id),
            resulting_scopes=await _default_scopes(session, str(role["code"])),
            principal_role_ids=principal_role_ids,
            principal_scopes=await _principal_scope_types(session, tenant_id, principal.user_id),
        )
        hits = await _guard_self_grant(
            principal,
            role=role,
            exposures=[("permission", permission_exposure), ("scope", scope_exposure)],
            self_grant_ack=self_grant_ack,
            reason=normalized_reason,
            denied_action="school.role.reset_denied",
            request_id=request_id,
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
        await _audit_self_grant_applied(
            session,
            principal,
            tenant_id=tenant_id,
            role=role,
            hits=hits,
            reason=normalized_reason,
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


def _integrity_constraint_name(exc: IntegrityError) -> str | None:
    """从完整性错误里取 PG 回报的约束名（错误对象的 `constraint_name`，源自协议字段 `n`）。

    名字在 `exc.orig.__cause__` 上，不在 `exc.orig` 上：SQLAlchemy 的 asyncpg 适配异常只搬运了
    `sqlstate`/`pgcode`/`detail`，`constraint_name` 留在驱动的原生异常里，所以先看适配层、
    再往下取一层原始异常。两个位置都取不到就返回 `None`（= 按"不是同码冲突"处理，宁可 500）。

    不用"在错误文本里找子串"：那种写法会把 `uq_roles_tenant_id_code_v2`、或 SQL 文本里恰好
    含这个名字的其它约束失败，一并误判成同码冲突。
    """

    orig: Any = exc.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        name: Any = getattr(candidate, "constraint_name", None)
        if name is not None:
            return str(name)
    return None


async def create_role(
    principal: Principal,
    *,
    code: str,
    name: str,
    permission_codes: list[str],
    copy_from_role_id: UUID | None,
    reason: str | None,
    request_id: str | None,
) -> RoleSummary:
    """在本租户内创建自定义角色（`is_system=false`），初始权限为空集或复制自同租户可编辑角色。

    不变量（简报 §4.1）：
    - `code` 由调用方给（Pydantic 已按 `ROLE_CODE_PATTERN` 校验），不得撞内置保留码或本租户既有码；
    - 权限集只能取租户域目录内的码（平台域 422），且非超管不得写入 `school.role.manage`（转授禁令）；
    - `is_system=false` → 永不参与同步补种与重置出厂（同步谓词限定 `r.is_system`）。

    **不接自扩权守卫**（2026-10-27 用户裁定，简报 §12）：新建角色此刻尚未绑定任何人，
    `role_id not in principal_role_ids` → 净增恒为空，判据结构上不可触发（创建本身不授予任何人能力，
    能力只在 PUT 配权限 / 用户↔角色绑定时才流动，那两条路径各自接守卫）。
    "建一个含敏感码的角色再绑给自己"这条链的墙是绑定接口的自绑禁令（简报决策 #5 的
    `BINDING_SELF_FORBIDDEN`）——**今日本仓还没有任何 `user_roles` 写路径**，那条禁令是 TD-187d
    的落地前置，不是已经存在的防线；别把它当成漏接线在这里补一个永不触发的分支。
    见简报决策 #4 的修正说明与 TD-194。

    异常:
        RoleAdminError: 409（码为内置保留/同码已存在/目录漂移）、404（复制源不存在/跨租）、
            422（复制源非可编辑角色、复制源与显式权限清单同时给出、非法权限码、
            非超管转授 `school.role.manage`）。
    """

    tenant_id = _require_tenant(principal)
    normalized_code = code.strip()
    normalized_name = name.strip()
    normalized_reason = reason.strip() if reason and reason.strip() else None

    if copy_from_role_id is not None and permission_codes:
        # 二选一（简报 §4.1）。不选"以显式清单为准、静默忽略复制源"：前端两个字段都填时
        # 调用方会以为复制的角色集生效了，实际拿到的是另一份权限集——配置类接口不能猜。
        # 判据用真值而不是"字段是否出现"：`permission_codes` 缺省即空集，**空清单 + 复制源按复制
        # 执行**是有意的——复制模式下前端的权限勾选框本来就是空的，把它判成非法等于逼调用方
        # 靠"少传一个字段"来表达意图。要拦的是"两份非空来源同时给"。
        raise RoleAdminError(
            "ROLE_INITIAL_SOURCE_AMBIGUOUS",
            "初始权限只能二选一：`permission_codes` 或 `copy_from_role_id`",
            422,
        )

    if normalized_code in RESERVED_TENANT_ROLE_CODES:
        await _audit_denied(
            principal,
            action="school.role.create_denied",
            resource_id=normalized_code,
            reason="ROLE_CODE_TAKEN",
            request_id=request_id,
        )
        raise RoleAdminError("ROLE_CODE_TAKEN", f"角色码 {normalized_code} 为内置保留码", 409)

    new_role_id = uuid4()
    async with db_session(tenant_id=tenant_id) as session:
        # 同码预检（内置角色开通时已入库，这里一并挡住"撞内置码"与"撞自建码"两种；
        # 真正的兜底是 `uq_roles_tenant_id_code`，并发落到那一步由下面 IntegrityError 分支接管）。
        exists = await session.scalar(
            text("select 1 from public.roles where tenant_id = :tid and code = :code"),
            {"tid": tenant_id, "code": normalized_code},
        )
        if exists:
            await _audit_denied(
                principal,
                action="school.role.create_denied",
                resource_id=normalized_code,
                reason="ROLE_CODE_TAKEN",
                request_id=request_id,
            )
            raise RoleAdminError("ROLE_CODE_TAKEN", "同码角色已存在", 409)

        if copy_from_role_id is not None:
            source = await _role_row(session, tenant_id, copy_from_role_id, for_update=False)
            if source is None:
                # 复制源是被探测的他人对象 id，按"越权/撞码留痕"一侧处理，与不存在同形 404（不暴露他租）。
                # `resource_id` 记要新建的码（本请求的目标对象），被探测的 role id 进 metadata：
                # 巡检要能回答"谁在拿别人家的 role id 枚举"，只看新码是看不出来的。
                await _audit_denied(
                    principal,
                    action="school.role.create_denied",
                    resource_id=normalized_code,
                    reason="ROLE_NOT_FOUND",
                    request_id=request_id,
                    extra={"copy_from_role_id": str(copy_from_role_id)},
                )
                raise RoleAdminError("ROLE_NOT_FOUND", "复制源角色不存在", 404)
            if _lock_reason(str(source["code"]), is_system=bool(source["is_system"])) is not None:
                # 锁定角色（超管/C 端）不能当复制源：把超管全量搬进自定义角色再绑给自己，
                # 会绕开"只能从可编辑角色起步"的配置直觉。人读 422，而不是让它悄悄复制成功。
                await _audit_denied(
                    principal,
                    action="school.role.create_denied",
                    resource_id=normalized_code,
                    reason="ROLE_COPY_SOURCE_NOT_EDITABLE",
                    request_id=request_id,
                )
                raise RoleAdminError(
                    "ROLE_COPY_SOURCE_NOT_EDITABLE", "复制源必须是同租户内的可编辑角色", 422
                )
            initial_codes = sorted(
                await _role_permission_codes(session, tenant_id, copy_from_role_id)
            )
        else:
            initial_codes = sorted(set(permission_codes))

        _validate_codes(initial_codes)
        try:
            _assert_delegable(principal, initial_codes)
        except RoleAdminError as exc:
            await _audit_denied(
                principal,
                action="school.role.create_denied",
                resource_id=normalized_code,
                reason=exc.code,
                request_id=request_id,
            )
            raise
        if initial_codes:
            await _assert_codes_in_catalog(session, set(initial_codes))

        try:
            await session.execute(
                text(
                    "insert into public.roles (id, tenant_id, code, name, is_system, "
                    "defaults_applied, version) "
                    "values (cast(:id as uuid), cast(:tid as uuid), :code, :name, "
                    "false, false, 0)"
                ),
                {"id": new_role_id, "tid": tenant_id, "code": normalized_code, "name": normalized_name},
            )
        except IntegrityError as exc:
            # 并发建同名码：预检与 INSERT 之间的窗口靠唯一约束兜底，翻成人读 409 而不是 500。
            # 但只认这一条约束：格式 CHECK / FK / NOT NULL 被踩到说明库或迁移有真故障，一律报
            # "同码已存在"会把数据不一致伪装成业务冲突，排障的人会去查角色表而不是查迁移。
            if _integrity_constraint_name(exc) != _ROLE_CODE_UNIQUE_CONSTRAINT:
                raise
            await _audit_denied(
                principal,
                action="school.role.create_denied",
                resource_id=normalized_code,
                reason="ROLE_CODE_TAKEN",
                request_id=request_id,
            )
            raise RoleAdminError("ROLE_CODE_TAKEN", "同码角色已存在（并发）", 409) from exc

        if initial_codes:
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
                        {"tid": tenant_id, "rid": new_role_id, "codes": initial_codes},
                    )
                )
                .scalars()
                .all()
            )
            if len(inserted) != len(initial_codes):
                raise RoleAdminError(
                    "PERMISSION_CATALOG_OUT_OF_SYNC",
                    "权限目录与数据库不一致，请先执行 RBAC 同步",
                    409,
                )

        await append_audit(
            session,
            action="school.role.created",
            resource_type="role",
            resource_id=str(new_role_id),
            actor_realm="tenant",
            actor_id=principal.user_id,
            tenant_id=tenant_id,
            metadata={
                "role_code": normalized_code,
                "name": normalized_name,
                "permission_codes": initial_codes,
                "copy_from_role_id": str(copy_from_role_id) if copy_from_role_id is not None else None,
                "reason": normalized_reason,
            },
            request_id=request_id,
        )
        return await _summary(session, tenant_id, new_role_id)


async def preview_role_deletion(principal: Principal, role_id: UUID) -> RoleDeletionPreview:
    """只读预览删除自定义角色的影响面（不写库、不审计；与重置预览同口径）。

    `affected_users` 与 `fingerprint` 都是**预览时刻**的值：绑定关系（`user_roles`）的写入不推进
    `roles.version`，所以预览→确认之间新绑的人不会被版本 CAS 挡下；执行侧重新计算指纹并比对，
    不一致即 409 `ROLE_DELETE_PREVIEW_STALE` 并回带最新预览（TD-199，简报 §14.3）。

    异常:
        RoleAdminError: 404（不存在/跨租）、409（内置角色不可删）。
    """

    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        role = await _role_row(session, tenant_id, role_id, for_update=False)
        if role is None:
            raise RoleAdminError("ROLE_NOT_FOUND", "角色不存在", 404)
        if bool(role["is_system"]):
            raise RoleAdminError("ROLE_NOT_DELETABLE", "内置角色不可删除", 409)
        affected_ids = await _affected_user_ids(session, tenant_id, role_id)
        return RoleDeletionPreview(
            role_code=str(role["code"]),
            name=str(role["name"]),
            version=int(role["version"]),
            affected_users=len(affected_ids),
            permission_codes=tuple(
                sorted(await _role_permission_codes(session, tenant_id, role_id))
            ),
            fingerprint=_deletion_fingerprint(role_id, affected_ids),
        )


async def delete_role(
    principal: Principal,
    role_id: UUID,
    *,
    confirm_role_code: str,
    reason: str,
    version: int,
    fingerprint: str,
    request_id: str | None,
) -> RoleDeletionPreview:
    """硬删自定义角色（简报 §4.2 方案 A）：预览影响面 → 逐字确认码 + 理由 ≥10 + 双判据 CAS。

    两套判据各管一件事（TD-199，简报 §14.3）：
    - `version`：预览后角色的权限/范围被改（`roles.version` 在那些写路径上递增）；
    - `fingerprint`：预览后持有人集合被改（绑定不推进角色版本，只能靠指纹）。
    两者任一不匹配都拒绝，`ROLE_VERSION_CONFLICT` / `ROLE_DELETE_PREVIEW_STALE` 分流，
    后者回带最新删除预览。指纹在 `FOR UPDATE` 行锁之后重算——绑定插入会因外键的
    `FOR KEY SHARE` 锁等待或先行提交，不会出现"按旧人数删掉、绑定同时成功"。

    子表 `role_permissions`/`data_scopes`/`user_roles` 由复合外键 `ON DELETE CASCADE` 清理，
    所以"删除一个角色"是单条 `DELETE`，但**必须在删除前把权限码集与受影响用户数快照进审计**
    （CASCADE 之后取不回）。
    内置角色一律 409 `ROLE_NOT_DELETABLE`（只能重置或改配置）。
    响应里的 `version` 是**被删那一行的最后版本号**，不是 `+1`：行已经不在了，报一个从未存在过的
    版本只会诱导前端拿它去做下一次乐观锁。

    异常:
        RoleAdminError: 404（不存在/跨租）、409（内置角色不可删/版本冲突/影响面已变化）、
            422（确认码不符/理由过短）。
    """

    tenant_id = _require_tenant(principal)
    normalized_reason = reason.strip()
    if len(normalized_reason) < 10:
        raise RoleAdminError("ROLE_DELETE_REASON_REQUIRED", "删除理由至少 10 个字符", 422)

    async with db_session(tenant_id=tenant_id) as session:
        role = await _role_row(session, tenant_id, role_id, for_update=True)
        if role is None:
            await _audit_denied(
                principal,
                action="school.role.delete_denied",
                resource_id=str(role_id),
                reason="ROLE_NOT_FOUND",
                request_id=request_id,
            )
            raise RoleAdminError("ROLE_NOT_FOUND", "角色不存在", 404)
        if bool(role["is_system"]):
            await _audit_denied(
                principal,
                action="school.role.delete_denied",
                resource_id=str(role_id),
                reason="ROLE_NOT_DELETABLE",
                request_id=request_id,
            )
            raise RoleAdminError("ROLE_NOT_DELETABLE", "内置角色不可删除，只能重置或改配置", 409)
        if str(role["code"]) != confirm_role_code.strip():
            # 用错了（确认码打错）而非越权：不落安全审计，与 ROLE_CODE_MISMATCH 同口径。
            raise RoleAdminError("ROLE_CODE_MISMATCH", "确认角色码与目标角色不一致", 422)
        if int(role["version"]) != version:
            latest = await _summary(session, tenant_id, role_id)
            raise RoleAdminError(
                "ROLE_VERSION_CONFLICT", "角色已被他人修改，请刷新后重试", 409, latest=latest
            )

        role_code = str(role["code"])
        role_name = str(role["name"])
        # 删除前快照：CASCADE 之后权限与绑定都消失，审计是唯一的复盘来源。
        before = await _role_permission_codes(session, tenant_id, role_id)
        affected_ids = await _affected_user_ids(session, tenant_id, role_id, for_update=True)
        current_fingerprint = _deletion_fingerprint(role_id, affected_ids)
        if current_fingerprint != fingerprint:
            # 并发绑定不是安全事件（同事在正常配置权限账号），只回 409 + 最新影响面，
            # 不写拒绝审计——与 ROLE_VERSION_CONFLICT 同口径。
            raise RoleAdminError(
                "ROLE_DELETE_PREVIEW_STALE",
                "删除影响面已变化（持有人集合与预览不一致），请刷新后重新确认",
                409,
                latest_preview=RoleDeletionPreview(
                    role_code=role_code,
                    name=role_name,
                    version=int(role["version"]),
                    affected_users=len(affected_ids),
                    permission_codes=tuple(sorted(before)),
                    fingerprint=current_fingerprint,
                ),
            )
        # 已持 FOR UPDATE 行锁，同事务内并发不可能改走版本或删除该行，故 WHERE 只按 id+tenant。
        # rowcount 仍要断言 1：RLS 的 USING 子句若挡下这条 DELETE，语句会**静默影响 0 行**，
        # 而下一步的审计会写成"已删除"——库里角色还在，审计却说没了，这是最坏的一致性组合。
        deleted: Any = await session.execute(
            text("delete from public.roles where id = :rid and tenant_id = :tid"),
            {"rid": role_id, "tid": tenant_id},
        )
        if deleted.rowcount != 1:
            raise RoleAdminError("ROLE_NOT_FOUND", "角色不存在或已被删除", 404)

        await append_audit(
            session,
            action="school.role.deleted",
            resource_type="role",
            resource_id=str(role_id),
            actor_realm="tenant",
            actor_id=principal.user_id,
            tenant_id=tenant_id,
            metadata={
                "role_code": role_code,
                "name": role_name,
                "removed_permission_codes": sorted(before),
                "affected_users": len(affected_ids),
                "reason": normalized_reason,
            },
            request_id=request_id,
        )
        return RoleDeletionPreview(
            role_code=role_code,
            name=role_name,
            version=int(role["version"]),
            affected_users=len(affected_ids),
            permission_codes=tuple(sorted(before)),
            fingerprint=current_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class RoleScopeRow:
    """角色上的一条范围行。

    `editable=False` 表示这行不归角色配置这条写路径：`class` 需要绑定具体班级 id、本端点没有
    那个入参通道（用户级班级行才归 `app/org/scopes_sync.py` 的差量同步所有）。判据留在服务端
    下发，不让前端按范围类型名字猜——猜就是第二处事实来源，与 TD-200 同一口径。
    """

    scope_type: str
    scope_id: str | None
    editable: bool


@dataclass(frozen=True, slots=True)
class ScopeChangePreview:
    """一次范围替换的事前影响面（只读、不审计）。"""

    version: int
    added: tuple[str, ...]
    removed: tuple[str, ...]
    affected_users: int
    #: 替换后该角色一条范围都没有——持有人会看到空集（解析层 fail-closed），需要提示
    empties_scope: bool
    #: 本次不写、也不会被删掉的非可编辑行（缺 `scope_id` 通道，本端点不碰），列出来免得"结果集"被误读
    retained_non_editable: tuple[str, ...]
    newly_reaching_self: tuple[str, ...]
    requires_confirmation: bool


def _assert_known_scope_types(scope_types: set[str]) -> None:
    """字面量必须是模型声明的 `ScopeType` 之一；未知即入参非法（按四类分档**不留痕**）。

    先于"可编辑性"判定：`{'bogus','class'}` 报 UNKNOWN 而不是 NOT_EDITABLE——可编辑性要先认识
    这个类型才谈得上。清单来自 `app/rbac/scopes.py:KNOWN_SCOPE_TYPES`（直接取模型的
    `ScopeType` 参数化类型），不在这里再抄一份字面量。
    """

    unknown = sorted(scope_types - KNOWN_SCOPE_TYPES)
    if unknown:
        raise RoleAdminError("UNKNOWN_SCOPE_TYPE", f"未知数据范围类型：{'、'.join(unknown)}", 422)


def _assert_editable_scope_types(scope_types: set[str]) -> None:
    """`class`/`course` 不可手工授：**留痕**（简报 §13.2 决策③）。

    这一条与"目录外权限码"不同族——后者是填错了字面量，前者拦的是"往库里写一条本端点补不全
    的行"：这两类必须绑定具体班级/课程 id（库 CHECK
    `ck_data_scopes_scope_id_required_for_class_course`），而本端点的入参只有"类型"这一维、没有
    "选哪个班"的通道，真写下去要么撞 CHECK，要么造出一条解析层读不懂的行（持有人每次请求在
    `scope_context_from_rows` 抛 ValueError → 500）。用户级班级行才归 `app/org/scopes_sync.py`
    的差量同步所有，它只处理 `user_id is not null` 的行；角色级同型行今天没有任何合法写入方。
    判据看的是"谁在试图扩"，所以按 `ROLE_CODE_NOT_DELEGABLE` 的处理走 `*.denied`。
    """

    blocked = sorted(scope_types - EDITABLE_SCOPE_TYPES)
    if not blocked:
        return
    reasons = "；".join(
        f"{item}：{_SCOPE_TYPE_OWNERSHIP.get(item, '不由角色配置写入')}" for item in blocked
    )
    raise RoleAdminError("SCOPE_TYPE_NOT_EDITABLE", f"这些数据范围不由角色配置写入：{reasons}", 422)


async def _role_scope_rows(
    session: AsyncSession, tenant_id: UUID, role_id: UUID
) -> list[RoleScopeRow]:
    rows: list[Any] = list(
        (
            await session.execute(
                text(
                    "select scope_type, scope_id::text as scope_id from public.data_scopes "
                    "where role_id = :rid and tenant_id = :tid "
                    "order by scope_type, scope_id nulls first"
                ),
                {"rid": role_id, "tid": tenant_id},
            )
        )
        .mappings()
        .all()
    )
    return [
        RoleScopeRow(
            scope_type=str(row["scope_type"]),
            scope_id=None if row["scope_id"] is None else str(row["scope_id"]),
            editable=str(row["scope_type"]) in EDITABLE_SCOPE_TYPES,
        )
        for row in rows
    ]


def _resulting_scope_types(submitted: set[str], existing: list[RoleScopeRow]) -> set[str]:
    """全量替换的真实结果集 = 提交的清单 ∪ 本次**不碰**的非可编辑行。

    只按提交清单算结果集会低估守卫看到的范围、也会让预览的"新增/移除"说谎：非可编辑行
    不参与删除（见 `replace_role_scopes` 里那条窄删除），它们替换后仍然生效。
    """

    return set(submitted) | {row.scope_type for row in existing if not row.editable}


async def list_role_scopes(principal: Principal, role_id: UUID) -> list[RoleScopeRow]:
    """该角色当前的范围行（含只读行，供配置页渲染）。不存在/跨租同形 404、读路径不留痕。"""

    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        role = await _role_row(session, tenant_id, role_id, for_update=False)
        if role is None:
            raise RoleAdminError("ROLE_NOT_FOUND", "角色不存在", 404)
        return await _role_scope_rows(session, tenant_id, role_id)


async def preview_role_scopes(
    principal: Principal, role_id: UUID, *, scope_types: set[str]
) -> ScopeChangePreview:
    """范围替换的事前影响面：差异、持有人数、是否触达本人。只读，不审计、不推高版本。

    类型族两档与 `PUT` **同码、相对顺序也同**（未知 → 不可手工授），否则"预览说能过、保存被拒"
    会让运营当预览是装饰。整体顺序**有意不同**且是结构性的：`PUT` 的"角色存在/可编辑"夹在两档
    之间（要先 `FOR UPDATE` 拿到行才判得了角色），预览不锁行、也不做版本比较，所以三档判据一次性
    放在取行之前——"不存在的角色 + `class`"预览给 422 而 `PUT` 给 404 留痕。预览阶段被拒不落安全
    审计：那时还没有任何写入意图落地，反复试探由写路径那条 `*.denied` 计数。
    """

    tenant_id = _require_tenant(principal)
    _assert_known_scope_types(scope_types)
    _assert_editable_scope_types(scope_types)
    async with db_session(tenant_id=tenant_id) as session:
        role = await _role_row(session, tenant_id, role_id, for_update=False)
        if role is None:
            raise RoleAdminError("ROLE_NOT_FOUND", "角色不存在", 404)
        _assert_editable(role)
        existing = await _role_scope_rows(session, tenant_id, role_id)
        resulting = _resulting_scope_types(scope_types, existing)
        before = {row.scope_type for row in existing}
        exposure = evaluate_scope_self_grant(
            role_id=str(role_id),
            resulting_scopes=resulting,
            principal_role_ids=await _principal_role_ids(session, tenant_id, principal.user_id),
            principal_scopes=await _principal_scope_types(session, tenant_id, principal.user_id),
        )
        return ScopeChangePreview(
            version=int(role["version"]),
            added=tuple(sorted(resulting - before)),
            removed=tuple(sorted(before - resulting)),
            affected_users=await _affected_users(session, tenant_id, role_id),
            empties_scope=not resulting,
            retained_non_editable=tuple(sorted(r.scope_type for r in existing if not r.editable)),
            newly_reaching_self=exposure.newly_reaching_self,
            requires_confirmation=exposure.requires_confirmation,
        )


async def replace_role_scopes(
    principal: Principal,
    role_id: UUID,
    *,
    scope_types: set[str],
    version: int,
    reason: str | None,
    request_id: str | None,
    self_grant_ack: bool = False,
) -> RoleSummary:
    """全量替换角色级数据范围（版本 CAS + 审计 diff + 范围版自扩权确认，TD-187c）。

    与 `replace_role_permissions` 共享同一列 `roles.version`：一次权限保存会让另一侧的范围草稿
    409，这是有意的（分列版本会出现"权限已更新、范围更新失败"的半更新态），代价登记在 TD-202。

    异常:
        RoleAdminError: 404（不存在/跨租）、409（锁定角色、版本冲突、类型与库 CHECK 漂移）、
            422（`UNKNOWN_SCOPE_TYPE`、`SCOPE_TYPE_NOT_EDITABLE`、自扩权未确认/未填理由）。
    """

    tenant_id = _require_tenant(principal)
    _assert_known_scope_types(scope_types)
    submitted = sorted(scope_types)
    normalized_reason = reason.strip() if reason and reason.strip() else None

    async with db_session(tenant_id=tenant_id) as session:
        role = await _role_row(session, tenant_id, role_id, for_update=True)
        if role is None:
            # 写路径的 id 探测按"越权/撞码"一侧留痕，与权限替换同口径
            await _audit_denied(
                principal,
                action="school.role.scopes.denied",
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
                action="school.role.scopes.denied",
                resource_id=str(role_id),
                reason=exc.code,
                request_id=request_id,
            )
            raise
        try:
            _assert_editable_scope_types(scope_types)
        except RoleAdminError as exc:
            # 试图手工授 `class`/`course` = 试图写一条本端点无法补全 `scope_id` 的范围行，
            # 判据看的是"谁在试图扩"，留痕（简报 §13.2 ③）
            await _audit_denied(
                principal,
                action="school.role.scopes.denied",
                resource_id=str(role_id),
                reason=exc.code,
                request_id=request_id,
                extra={"requested_scope_types": submitted},
            )
            raise
        if int(role["version"]) != version:
            latest = await _summary(session, tenant_id, role_id)
            raise RoleAdminError(
                "ROLE_VERSION_CONFLICT", "角色已被他人修改，请刷新后重试", 409, latest=latest
            )

        existing = await _role_scope_rows(session, tenant_id, role_id)
        before = {row.scope_type for row in existing}
        resulting = _resulting_scope_types(scope_types, existing)
        # 范围版自扩权判定同样排在版本 CAS 之前：被拒的请求不得推高 `roles.version`
        exposure = evaluate_scope_self_grant(
            role_id=str(role_id),
            resulting_scopes=resulting,
            principal_role_ids=await _principal_role_ids(session, tenant_id, principal.user_id),
            principal_scopes=await _principal_scope_types(session, tenant_id, principal.user_id),
        )
        hits = await _guard_self_grant(
            principal,
            role=role,
            exposures=[("scope", exposure)],
            self_grant_ack=self_grant_ack,
            reason=normalized_reason,
            denied_action="school.role.scopes.denied",
            request_id=request_id,
        )

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

        # 删除只限可编辑的那两类：非可编辑行（角色级 `class` 同型行）不归这条写路径所有，
        # 全删等于替一个今天还不存在的写入方做清理，预览的"结果集"也会因此说谎。
        await session.execute(
            text(
                "delete from public.data_scopes "
                "where role_id = :rid and tenant_id = :tid "
                "and scope_type = any(cast(:types as text[]))"
            ),
            {
                "rid": role_id,
                "tid": tenant_id,
                "types": sorted(EDITABLE_SCOPE_TYPES),
            },
        )
        if submitted:
            try:
                inserted: list[Any] = list(
                    (
                        await session.execute(
                            text(
                                "insert into public.data_scopes "
                                "(id, tenant_id, role_id, scope_type, scope_id) "
                                "select gen_random_uuid(), :tid, :rid, s.scope_type, null "
                                "from unnest(cast(:types as text[])) as s(scope_type) "
                                "returning scope_type"
                            ),
                            {"tid": tenant_id, "rid": role_id, "types": submitted},
                        )
                    )
                    .scalars()
                    .all()
                )
            except IntegrityError as exc:
                # `KNOWN_SCOPE_TYPES` 来自模型的 `ScopeType`，库内那条 CHECK 来自迁移——只有
                # 两边漂移时才会走到这里（模型加了类型、迁移没跟上）。
                # 只翻译这一条约束，其余完整性错误原样抛出（同 187b 的并发兜底口径）。
                if _integrity_constraint_name(exc) != _SCOPE_TYPE_CHECK_CONSTRAINT:
                    raise
                raise RoleAdminError(
                    "SCOPE_CATALOG_OUT_OF_SYNC",
                    "数据范围类型与数据库约束不一致，请先执行迁移",
                    409,
                ) from exc
            if len(inserted) != len(submitted):
                raise RoleAdminError(
                    "SCOPE_CATALOG_OUT_OF_SYNC", "数据范围类型与数据库约束不一致，请先执行迁移", 409
                )

        await append_audit(
            session,
            action="school.role.scopes.updated",
            resource_type="role",
            resource_id=str(role_id),
            actor_realm="tenant",
            actor_id=principal.user_id,
            tenant_id=tenant_id,
            metadata={
                "role_code": str(role["code"]),
                "added": sorted(resulting - before),
                "removed": sorted(before - resulting),
                "resulting_scopes": sorted(resulting),
                "affected_users": await _affected_users(session, tenant_id, role_id),
                "reason": normalized_reason,
            },
            request_id=request_id,
        )
        if exposure.requires_confirmation:
            await _audit_self_grant_applied(
                session,
                principal,
                tenant_id=tenant_id,
                role=role,
                hits=hits,
                reason=normalized_reason,
                request_id=request_id,
            )
        return await _summary(session, tenant_id, role_id)


__all__ = [
    "EDITABLE_ROLE_CODES",
    "EDITABLE_SCOPE_TYPES",
    "LOCKED_ROLE_REASONS",
    "NON_DELEGABLE_CODES",
    "RESERVED_TENANT_ROLE_CODES",
    "ROLE_CODE_PATTERN",
    "SUPER_ADMIN_ROLE_CODE",
    "TENANT_PERMISSIONS",
    "ResetPreview",
    "RoleAdminError",
    "RoleDeletionPreview",
    "RoleScopeRow",
    "RoleSummary",
    "ScopeChangePreview",
    "create_role",
    "delete_role",
    "list_permissions",
    "list_role_scopes",
    "list_roles",
    "preview_role_deletion",
    "preview_role_reset",
    "preview_role_scopes",
    "replace_role_permissions",
    "replace_role_scopes",
    "reset_role",
    "role_is_bindable",
]
