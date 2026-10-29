"""租户侧 MFA 管理员重置（TD-126）。

口径（`docs/design-briefs/M2-UI2-rbac-admin.md` §5）：

- 仅持 `school.personnel.mfa.reset` 的校方主体可用；先按登录名**精确定位**再重置，
  查询本身留审计（防枚举/滥用，限流见 M6）；
- 不能重置自己（自助走恢复码/关闭 MFA）、不能重置校方超管（由平台 CLI 兜底）、
  不能经此接口触碰平台账号（租户连接 + users 表双重隔离）；
- 重置即账号级安全事件：撤销全部**未撤销**的刷新家族（含已过期还没清理的死行）、
  清空 MFA 与锁定状态、`token_version+1`；
  审计里的 `sessions_revoked` 只数**还活着**的家族（未撤销且未过期），与停用侧、
  设备列表取数口径一致——"这次断了几台设备"要能跟台账对上（TD-231）；
  提交后删除 Redis 会话标记与待激活 secret；日志/审计不含 secret、恢复码或登录名明文；
- 取账号级 advisory lock **之前**先做一次无锁的本租归属探针，且探针跑在自己的短事务里
  （跑完即归还连接，未命中的 404 不会连同拒绝审计占住两条连接）：那把锁的 key 只有
  `realm + user_id`、不含租户，先取锁会让一次注定 404 的跨租请求排进**别租账号**的
  登录/刷新队列（TD-230，与 `app/personnel/account_admin.py` 同口径）。
"""

import logging
from dataclasses import dataclass
from typing import Any
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

logger = logging.getLogger(__name__)


class MfaAdminError(RuntimeError):
    """MFA 管理员操作错误（映射为 HTTP 状态码）。"""

    def __init__(self, code: str, message: str, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class TenantUserLookup:
    """按登录名定位到的租户用户（不含登录名/邮箱/身份证/MFA 密钥）。"""

    id: UUID
    display_name: str | None
    user_type: str
    status: str
    mfa_enabled: bool
    roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MfaResetResult:
    """重置结果（撤销的刷新家族数）。"""

    sessions_revoked: int


def _require_tenant(principal: Principal) -> UUID:
    if principal.realm != "tenant" or principal.tenant_id is None:
        raise MfaAdminError("REALM_MISMATCH", "该接口只在校方租户上下文内可用", 403)
    return principal.tenant_id


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
    action: str,
    resource_id: UUID | None,
    metadata: dict[str, Any],
    request_id: str | None,
) -> None:
    """被拒的 MFA 管理操作留痕（独立事务，best-effort）。

    服务层在写事务内 raise 时外层整体回滚，同事务写的拒绝审计会一起消失；而"谁试图重置
    超管 / 查了个不存在的登录名"恰是防枚举与滥用排查要看的东西。审计故障不改变拒绝结果。
    `metadata` 只放判据（found/reason/错误码），不得放登录名等明文。
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
                resource_id=None if resource_id is None else str(resource_id),
                actor_realm="tenant",
                actor_id=principal.user_id,
                tenant_id=tenant_id,
                result="denied",
                metadata=metadata,
                request_id=request_id,
            )
            flushed = True
    except Exception as exc:
        if flushed:
            # append_audit 已成功 flush，异常只能来自事务提交；它记不到这里，补一次计数。
            record_audit_write_failure(action)
        # 与 `personnel.service._audit_denied` 同口径：只记异常类型，不用 exc_info——
        # SQLAlchemy 会把 SQL 与参数拼进异常字符串（红线）。
        logger.warning(
            "MFA 管理拒绝审计写入失败：%s.%s",
            type(exc).__module__,
            type(exc).__name__,
            extra={"action": action},
        )
        return


async def lookup_user_by_login_id(
    principal: Principal, login_id: str, *, request_id: str | None = None
) -> TenantUserLookup:
    """按登录名精确定位本校用户；命中与未命中都留审计（不写登录名明文）。

    异常:
        MfaAdminError: 403（主体 realm 不符）、404（不存在/跨租）、422（登录名为空）。
    """

    tenant_id = _require_tenant(principal)
    normalized = login_id.strip()
    if not normalized:
        raise MfaAdminError("LOGIN_ID_REQUIRED", "登录名不能为空", 422)

    async with db_session(tenant_id=tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "select id, display_name, user_type, status, mfa_enabled "
                    "from public.users where tenant_id = :tid and login_id = :login"
                ),
                {"tid": tenant_id, "login": normalized},
            )
        ).mappings().one_or_none()
        roles = (
            await _role_codes(session, tenant_id, UUID(str(row["id"]))) if row is not None else set()
        )
        if row is not None:
            await append_audit(
                session,
                action="school.user.lookup",
                resource_type="user",
                resource_id=str(row["id"]),
                actor_realm="tenant",
                actor_id=principal.user_id,
                tenant_id=tenant_id,
                result="success",
                metadata={"found": True},
                request_id=request_id,
            )

    if row is None:
        # 未命中必须在**独立事务**留痕：本事务接下来 raise 404 会整体回滚，同事务写的
        # "查了个不存在的登录名"跟着消失，枚举探测就再也看不出痕迹。
        await _audit_denied(
            principal,
            action="school.user.lookup",
            resource_id=None,
            metadata={"found": False},
            request_id=request_id,
        )
        raise MfaAdminError("USER_NOT_FOUND", "用户不存在", 404)
    return TenantUserLookup(
        id=UUID(str(row["id"])),
        display_name=None if row["display_name"] is None else str(row["display_name"]),
        user_type=str(row["user_type"]),
        status=str(row["status"]),
        mfa_enabled=bool(row["mfa_enabled"]),
        roles=tuple(sorted(roles)),
    )


async def reset_user_mfa(
    principal: Principal,
    user_id: UUID,
    *,
    reason: str,
    request_id: str | None = None,
) -> MfaResetResult:
    """清空目标用户 MFA 绑定并轮换其全部会话（密码不变）。

    异常:
        MfaAdminError: 403（realm 不符/目标超管）、404（不存在/跨租）、
            409（重置自己/未启用 MFA）、422（理由过短）。
    """

    tenant_id = _require_tenant(principal)
    normalized_reason = reason.strip()
    if len(normalized_reason) < 10:
        raise MfaAdminError("RESET_REASON_REQUIRED", "重置理由至少 10 个字符", 422)
    if principal.user_id == user_id:
        await _audit_denied(
            principal,
            action="school.user.mfa_reset_denied",
            resource_id=user_id,
            metadata={"reason": "MFA_SELF_RESET"},
            request_id=request_id,
        )
        raise MfaAdminError(
            "MFA_SELF_RESET", "不能重置自己的 MFA；请使用恢复码或自助关闭", 409
        )

    # 取账号锁之前先做一次**不带锁**的本租归属探针，且探针跑在**自己的短事务**里（跑完即归还
    # 连接），与 `account_admin._change_status` 同一口径。advisory lock 的 key 只有
    # `realm + user_id`、不含 tenant_id，让跨租 id 直接进锁区间，本事务就会排在**别租那个账号**
    # 的登录/刷新事务后面——404 的耗时既泄露"这个 id 存在且正在写会话"，反复提交还能定向拖慢
    # 那条登录链路；探针若挂在主事务里，未命中的 404 还会连同拒绝审计占住两条连接。
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
            action="school.user.mfa_reset_denied",
            resource_id=user_id,
            metadata={"reason": "USER_NOT_FOUND"},
            request_id=request_id,
        )
        raise MfaAdminError("USER_NOT_FOUND", "用户不存在", 404)

    async with db_session(tenant_id=tenant_id) as session:
        await acquire_user_lock(session, realm="tenant", user_id=user_id)
        row = (
            await session.execute(
                text(
                    "select id, mfa_enabled from public.users "
                    "where id = :uid and tenant_id = :tid for update"
                ),
                {"uid": user_id, "tid": tenant_id},
            )
        ).mappings().one_or_none()
        if row is None:
            await _audit_denied(
                principal,
                action="school.user.mfa_reset_denied",
                resource_id=user_id,
                metadata={"reason": "USER_NOT_FOUND"},
                request_id=request_id,
            )
            raise MfaAdminError("USER_NOT_FOUND", "用户不存在", 404)
        roles = await _role_codes(session, tenant_id, user_id)
        if SUPER_ADMIN_ROLE_CODE in roles:
            await _audit_denied(
                principal,
                action="school.user.mfa_reset_denied",
                resource_id=user_id,
                metadata={"reason": "MFA_SUPER_ADMIN_FORBIDDEN"},
                request_id=request_id,
            )
            raise MfaAdminError(
                "MFA_SUPER_ADMIN_FORBIDDEN",
                "校方超管的 MFA 重置需由平台运维执行（CLI 兜底）",
                403,
            )
        if not bool(row["mfa_enabled"]):
            await _audit_denied(
                principal,
                action="school.user.mfa_reset_denied",
                resource_id=user_id,
                metadata={"reason": "MFA_NOT_ENABLED"},
                request_id=request_id,
            )
            raise MfaAdminError("MFA_NOT_ENABLED", "该用户未启用 MFA", 409)

        families: list[UUID] = list(
            (
                await session.execute(
                    text(
                        # 只数**还活着**的家族（未撤销且未过期），与停用侧口径一致：
                        # 审计里的 `sessions_revoked` = "断了几台设备"，死行不占数（TD-231）。
                        # 撤销语句仍是全量未撤销——把死行一并标上不花钱，但计数要准。
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
                "where user_id = :uid and tenant_id = :tid and revoked_at is null"
            ),
            {"uid": user_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "delete from public.mfa_recovery_codes "
                "where user_id = :uid and tenant_id = :tid"
            ),
            {"uid": user_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "update public.users set mfa_enabled = false, mfa_secret_enc = null, "
                "mfa_last_used_step = null, failed_login_attempts = 0, "
                "failed_login_window_started_at = null, locked_until = null, "
                "token_version = token_version + 1, updated_at = now() "
                "where id = :uid and tenant_id = :tid"
            ),
            {"uid": user_id, "tid": tenant_id},
        )
        await append_audit(
            session,
            action="school.user.mfa_reset",
            resource_type="user",
            resource_id=str(user_id),
            actor_realm="tenant",
            actor_id=principal.user_id,
            tenant_id=tenant_id,
            metadata={
                "sessions_revoked": len(families),
                "reason": normalized_reason,
            },
            request_id=request_id,
        )

    # Redis 清理在提交后执行：失败只影响「已签发 access 的即时失效窗口 ≤15 分钟」，
    # token_version 已递增，下一次请求仍会被拒绝（与平台 CLI 同一降级口径）。
    await revocation.delete_sessions(families, realm="tenant")
    await revocation.delete_mfa_pending_secret(realm="tenant", user_id=user_id)
    return MfaResetResult(sessions_revoked=len(families))


__all__ = [
    "MfaAdminError",
    "MfaResetResult",
    "TenantUserLookup",
    "lookup_user_by_login_id",
    "reset_user_mfa",
]
