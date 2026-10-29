"""租户侧权限账号停用 / 启用（TD-187e）。

口径（`docs/design-briefs/M2-UI3-custom-roles-accounts.md` §15）：

- 停用 = `status='disabled'` + 撤销**未撤销且未过期**的刷新家族（`revoked_reason='admin_revoke'`）
  + `token_version+1`，三件事各挡一个洞：状态挡登录/每请求鉴权/刷新，撤家族挡
  「重新启用后旧 refresh 复活」（启用不换 token 版本），版本递增让已签发的 access
  下一请求即 401。已过期家族不撤销也不计入 `sessions_revoked`（它本来就换不出令牌）。
  提交后删除 Redis 会话标记与 MFA 待激活 secret——那一步删不掉只会留下一个由 TTL 回收的
  孤儿标记：设备列表取数是「库内未撤销且未过期 ∩ Redis 标记存在」，不会多出一条，
  也不留续期路径（`revocation.delete_session` 吞 `RedisError`，停用仍 204）；
- 启用 = `status='active'` 并清空失败计数与锁定；**不动 MFA、不动角色绑定、不换 token**
  （启用不是重置，用户不该因为被停用过就丢掉已配置的安全要素）；
- 超管账号一律不由租户管理员启停（平台侧兜底，TD-224）；不能停用自己；停用必须填理由
  （≥10 字，去空白后计长），启用不需要理由；目标只能是「权限账号」（教师/校方管理员），
  与用户↔角色绑定共用 `BINDABLE_USER_TYPES`——学生是 C 端账号，不在这条权限的射程内；
- 状态迁移的顺序是「无锁本租归属探针（独立短事务）→ 账号级 advisory lock → `users FOR UPDATE` → 判据 → 写」：
  advisory 的 key 只有 `realm + user_id`、不含租户，所以归属必须先于取锁，否则本租的一次注定 404
  会排进**别租账号**的登录/刷新队列（TD-230）；探针跑在自己的事务里、跑完即归还连接，否则未命中的
  404 会连同拒绝审计同时占住两条连接（猜 id 的洪水请求走的就是这条路径）。两层锁各自独立承重
  （摘锁实测见简报 §15.6）：两个人同时停用只会有一次撤销与一条成功审计，只持锁不写行的会话事务同样会把停用挡在后面；
- 拒绝路径写 `school.user.status_denied`（独立事务、只落 `operation` 与判据码）；
  理由过短属入参非法，不落安全审计（与 `RESET_REASON_REQUIRED` 同口径，见 `design.md` §5.12）。
"""

import logging
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import append_audit
from app.auth import revocation
from app.auth.principal import Principal
from app.auth.sessions import acquire_user_lock
from app.core.metrics import record_audit_write_failure
from app.db.session import db_session
from app.rbac.admin import SUPER_ADMIN_ROLE_CODE
from app.rbac.binding import BINDABLE_USER_TYPES

logger = logging.getLogger(__name__)

DEACTIVATE_REASON_MIN_LENGTH = 10
StatusOperation = Literal["deactivate", "activate"]


class AccountAdminError(RuntimeError):
    """账号启停的业务错误（映射为 HTTP 状态码）。"""

    def __init__(self, code: str, message: str, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class StatusChangeResult:
    """启停结果（`sessions_revoked` 在启用方向恒为 0）。"""

    status: str
    status_before: str
    sessions_revoked: int


def _require_tenant(principal: Principal) -> UUID:
    if principal.realm != "tenant" or principal.tenant_id is None:
        raise AccountAdminError("REALM_MISMATCH", "该接口只在校方租户上下文内可用", 403)
    return principal.tenant_id


def normalize_deactivate_reason(reason: str) -> str:
    """停用理由去空白后校验长度；返回可直接写入审计的文本。"""

    normalized = reason.strip()
    if len(normalized) < DEACTIVATE_REASON_MIN_LENGTH:
        raise AccountAdminError(
            "DEACTIVATE_REASON_REQUIRED",
            f"停用理由至少 {DEACTIVATE_REASON_MIN_LENGTH} 个字符",
            422,
        )
    return normalized


def ensure_transition_allowed(status: str, *, operation: StatusOperation) -> str:
    """状态机（纯函数）：合法时返回目标状态，非法时抛业务错误。

    `deleted` 对两个方向都按"不存在"处理——删除后的账号不再是一个可管理的目标。
    """

    if status == "deleted":
        # 生产路径先在 `_change_status` 里把 `deleted` 收口成 404（**早于角色判据**，否则挂着
        # 超管绑定的软删账号会先撞 403、泄露"曾为超管"）。这里保留同一条不是冗余：状态机是纯
        # 函数，直接调用它的人拿不到那段顺序保证，删掉这行就等于把"删除即不存在"降级成约定。
        raise AccountAdminError("USER_NOT_FOUND", "用户不存在", 404)
    if operation == "deactivate":
        if status in {"active", "locked"}:
            return "disabled"
        raise AccountAdminError("ACCOUNT_ALREADY_DISABLED", "该账号已停用", 409)
    if status == "disabled":
        return "active"
    if status == "active":
        raise AccountAdminError("ACCOUNT_ALREADY_ACTIVE", "该账号已启用，无需重复启用", 409)
    raise AccountAdminError("ACCOUNT_NOT_DISABLED", "只有已停用的账号可以启用", 409)


async def _role_codes(session: AsyncSession, tenant_id: UUID, user_id: UUID) -> set[str]:
    rows: list[Any] = list(
        (
            await session.execute(
                text(
                    "select r.code from public.user_roles ur "
                    "join public.roles r on r.id = ur.role_id and r.tenant_id = ur.tenant_id "
                    "where ur.user_id = :uid and ur.tenant_id = :tid"
                ),
                {"uid": user_id, "tid": tenant_id},
            )
        )
        .scalars()
        .all()
    )
    return {str(code) for code in rows}


async def _audit_denied(
    principal: Principal,
    *,
    operation: StatusOperation,
    resource_id: UUID,
    reason: str,
    request_id: str | None,
) -> None:
    """被拒的启停操作留痕（独立事务，best-effort）。

    服务层在写事务内 raise 时外层整体回滚，同事务写的拒绝审计会跟着消失；而"谁在试图停用
    超管 / 拿别人家的 user id 枚举 / 重复撞状态机"恰是安全巡检要看的东西。审计故障不改变
    拒绝结果。`metadata` 只放判据码与操作方向，不得放登录名等明文。
    """

    tenant_id = principal.tenant_id
    if tenant_id is None:
        return
    flushed = False
    try:
        async with db_session(tenant_id=tenant_id) as session:
            await append_audit(
                session,
                action="school.user.status_denied",
                resource_type="user",
                resource_id=str(resource_id),
                actor_realm="tenant",
                actor_id=principal.user_id,
                tenant_id=tenant_id,
                result="denied",
                metadata={"operation": operation, "reason": reason},
                request_id=request_id,
            )
            flushed = True
    except Exception as exc:
        if flushed:
            # append_audit 已成功 flush，异常只能来自事务提交；它记不到这里，补一次计数。
            record_audit_write_failure("school.user.status_denied")
        # 与 `personnel.service._audit_denied` 同口径：只记异常类型，不用 exc_info——
        # SQLAlchemy 会把 SQL 与参数拼进异常字符串（红线）。
        logger.warning(
            "账号启停拒绝审计写入失败：%s.%s",
            type(exc).__module__,
            type(exc).__name__,
            extra={"operation": operation},
        )


async def deactivate_user(
    principal: Principal,
    user_id: UUID,
    *,
    reason: str,
    request_id: str | None = None,
) -> StatusChangeResult:
    """停用账号：状态转 `disabled`、撤销**未撤销且未过期**的刷新家族、`token_version+1`。

    异常:
        AccountAdminError: 403（realm 不符/目标超管）、404（不存在/跨租/已删除）、
            409（停用自己/重复停用）、422（理由过短 `DEACTIVATE_REASON_REQUIRED` /
            目标类型非教师·校方管理员 `ACCOUNT_TARGET_NOT_ALLOWED`）。
    """

    return await _change_status(
        principal,
        user_id,
        operation="deactivate",
        reason=reason,
        request_id=request_id,
    )


async def activate_user(
    principal: Principal,
    user_id: UUID,
    *,
    request_id: str | None = None,
) -> StatusChangeResult:
    """重新启用账号并清空锁定状态（不动 MFA、绑定与令牌版本）。

    异常:
        AccountAdminError: 403（realm 不符/目标超管）、404（不存在/跨租/已删除）、
            409（账号未停用/已启用）、422（目标类型非教师·校方管理员
            `ACCOUNT_TARGET_NOT_ALLOWED`——启用方向不需要理由，但目标类型判据同样生效）。
    """

    return await _change_status(
        principal,
        user_id,
        operation="activate",
        reason=None,
        request_id=request_id,
    )


async def _change_status(
    principal: Principal,
    user_id: UUID,
    *,
    operation: StatusOperation,
    reason: str | None,
    request_id: str | None,
) -> StatusChangeResult:
    tenant_id = _require_tenant(principal)
    normalized_reason = (
        normalize_deactivate_reason(reason or "") if operation == "deactivate" else None
    )
    if operation == "deactivate" and principal.user_id == user_id:
        await _audit_denied(
            principal,
            operation=operation,
            resource_id=user_id,
            reason="ACCOUNT_SELF_DEACTIVATE",
            request_id=request_id,
        )
        raise AccountAdminError("ACCOUNT_SELF_DEACTIVATE", "不能停用自己的账号", 409)

    # 取账号锁之前先做一次**不带锁**的本租归属探针，且探针跑在**自己的短事务**里（跑完即归还
    # 连接）。advisory lock 的 key 只有 `realm + user_id`（不含 tenant_id），若在跨租 id 上
    # 直接取锁，本事务会排进那个别租账号的登录/刷新队列：404 的耗时泄露"这个 id 存在且正在写
    # 会话"，反复提交还能定向拖慢那条链路。探针不放进主事务还有第二层原因——未命中的 404 要写
    # 拒绝审计（独立事务），两条连接同时挂在一次注定失败的请求上，正是"猜 id 的洪水请求"最擅长
    # 放大的地方（`app/db/session.py:46` 每引擎 10+10 条连接）。下面的 `FOR UPDATE` 仍是权威
    # 判定（探针与取锁之间账号被硬删照样 404）。
    async with db_session(tenant_id=tenant_id) as probe_session:
        belongs = (
            await probe_session.execute(
                text("select 1 from public.users where id = :uid and tenant_id = :tid"),
                {"uid": user_id, "tid": tenant_id},
            )
        ).scalar_one_or_none()
    if belongs is None:
        await _audit_denied(
            principal,
            operation=operation,
            resource_id=user_id,
            reason="USER_NOT_FOUND",
            request_id=request_id,
        )
        raise AccountAdminError("USER_NOT_FOUND", "用户不存在", 404)

    families: list[UUID] = []
    async with db_session(tenant_id=tenant_id) as session:
        await acquire_user_lock(session, realm="tenant", user_id=user_id)
        row = (
            await session.execute(
                text(
                    "select id, status, user_type from public.users "
                    "where id = :uid and tenant_id = :tid for update"
                ),
                {"uid": user_id, "tid": tenant_id},
            )
        ).mappings().one_or_none()
        if row is None:
            await _audit_denied(
                principal,
                operation=operation,
                resource_id=user_id,
                reason="USER_NOT_FOUND",
                request_id=request_id,
            )
            raise AccountAdminError("USER_NOT_FOUND", "用户不存在", 404)
        status_before = str(row["status"])
        # 已删除的目标先按"不存在"收口：否则一个仍持有超管绑定的软删账号会得到 403，
        # 既破坏"删除即 404"的契约，也泄露"这个 id 曾经是超管"。
        if status_before == "deleted":
            await _audit_denied(
                principal,
                operation=operation,
                resource_id=user_id,
                reason="USER_NOT_FOUND",
                request_id=request_id,
            )
            raise AccountAdminError("USER_NOT_FOUND", "用户不存在", 404)
        roles = await _role_codes(session, tenant_id, user_id)
        if SUPER_ADMIN_ROLE_CODE in roles:
            await _audit_denied(
                principal,
                operation=operation,
                resource_id=user_id,
                reason="ACCOUNT_SUPER_ADMIN_FORBIDDEN",
                request_id=request_id,
            )
            raise AccountAdminError(
                "ACCOUNT_SUPER_ADMIN_FORBIDDEN",
                "校方超管账号不能由租户管理员停用或启用",
                403,
            )
        user_type = str(row["user_type"])
        if user_type not in BINDABLE_USER_TYPES:
            await _audit_denied(
                principal,
                operation=operation,
                resource_id=user_id,
                reason="ACCOUNT_TARGET_NOT_ALLOWED",
                request_id=request_id,
            )
            raise AccountAdminError(
                "ACCOUNT_TARGET_NOT_ALLOWED", "权限账号只能是教师或校方管理员", 422
            )
        try:
            next_status = ensure_transition_allowed(status_before, operation=operation)
        except AccountAdminError as exc:
            await _audit_denied(
                principal,
                operation=operation,
                resource_id=user_id,
                reason=exc.code,
                request_id=request_id,
            )
            raise

        if operation == "deactivate":
            # 只撤**还活着**的家族（未撤销且未过期）：已过期家族本来就换不出新 access，把它们
            # 一起打上 `admin_revoke` 会让审计里的 `sessions_revoked` 高于真正被断开的设备数
            # （`list_sessions` 按 `expires_at > now()` 过滤，从来看不见它们），台账就说不清
            # "这次停了几台设备"。两条语句的谓词必须逐字一致；`now()` 在事务内恒定，
            # SELECT 与 UPDATE 之间不会漂。
            families = list(
                (
                    await session.execute(
                        text(
                            "select distinct family_id from public.refresh_tokens "
                            "where user_id = :uid and tenant_id = :tid "
                            "and revoked_at is null and expires_at > now()"
                        ),
                        {"uid": user_id, "tid": tenant_id},
                    )
                )
                .scalars()
                .all()
            )
            await session.execute(
                text(
                    "update public.refresh_tokens set revoked_at = now(), "
                    "revoked_reason = 'admin_revoke' "
                    "where user_id = :uid and tenant_id = :tid "
                    "and revoked_at is null and expires_at > now()"
                ),
                {"uid": user_id, "tid": tenant_id},
            )
            await session.execute(
                text(
                    "update public.users set status = :next_status, "
                    "token_version = token_version + 1, updated_at = now() "
                    "where id = :uid and tenant_id = :tid"
                ),
                {"uid": user_id, "tid": tenant_id, "next_status": next_status},
            )
            await append_audit(
                session,
                action="school.user.deactivated",
                resource_type="user",
                resource_id=str(user_id),
                actor_realm="tenant",
                actor_id=principal.user_id,
                tenant_id=tenant_id,
                metadata={
                    "status_before": status_before,
                    "sessions_revoked": len(families),
                    "reason": normalized_reason,
                },
                request_id=request_id,
            )
        else:
            await session.execute(
                text(
                    "update public.users set status = :next_status, "
                    "failed_login_attempts = 0, failed_login_window_started_at = null, "
                    "locked_until = null, updated_at = now() "
                    "where id = :uid and tenant_id = :tid"
                ),
                {
                    "uid": user_id,
                    "tid": tenant_id,
                    "next_status": next_status,
                },
            )
            await append_audit(
                session,
                action="school.user.activated",
                resource_type="user",
                resource_id=str(user_id),
                actor_realm="tenant",
                actor_id=principal.user_id,
                tenant_id=tenant_id,
                metadata={"status_before": status_before},
                request_id=request_id,
            )

    if operation == "deactivate":
        # Redis 清理在提交后执行：`delete_sessions` 自己吞 RedisError，所以这一步失败
        # 只会留下一个由 TTL 回收的孤儿标记——设备列表先按库内 revoked_at 过滤再与标记求交，
        # 不会多出一条；access/refresh/重新登录分别由库内 status 与 token_version 断开。
        await revocation.delete_sessions(families, realm="tenant")
        await revocation.delete_mfa_pending_secret(realm="tenant", user_id=user_id)
        return StatusChangeResult(
            status=next_status,
            status_before=status_before,
            sessions_revoked=len(families),
        )
    return StatusChangeResult(status=next_status, status_before=status_before, sessions_revoked=0)


__all__ = [
    "DEACTIVATE_REASON_MIN_LENGTH",
    "AccountAdminError",
    "StatusChangeResult",
    "activate_user",
    "deactivate_user",
    "ensure_transition_allowed",
    "normalize_deactivate_reason",
]
