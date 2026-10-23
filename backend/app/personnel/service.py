"""人员档案的只读访问（M1-E2，RBAC-02 服务端数据范围过滤）。

口径（`docs/design-briefs/M1-E2-data-scope.md` §0 ④⑥⑨⑩）：

- 范围谓词由 `TenantScopedRepository` 统一注入，本模块**不自己拼 where**；
- 列表：静默过滤——看不到的人不出现，不报错（口径 ⑩）；
- 详情：显式 id 越权 → **404**（不是 403：403 会告诉调用方"这个人存在但你不能看"），
  同时写一条 `data_scope.denied` 安全审计（元数据只有 id 与判定原因，不含任何档案内容）；
- 敏感字段（邮箱）脱敏返回；`school.personnel.sensitive.read` 的完整查看路径不在本期（TD-152）。
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import append_audit
from app.auth.principal import Principal
from app.db.models.identity import User
from app.db.repository import ScopeBypass, ScopeDecision, TenantScopedRepository
from app.db.session import db_session

# 列表分页上界：与 `campus` 侧其他列表接口同一口径，防止一次拉走整租户名册
MAX_PAGE_SIZE = 100

# 越权判定时"要知道这一行是否存在"的绕过理由（只读 id，不回传任何档案字段）。
# 写成常量是为了让守卫测试和复审都能一眼看到：这里的绕过只服务于 404/审计的分叉，不是放行数据。
_EXISTS_PROBE_REASON = "越权判定需要区分「不存在」与「存在但不可见」，只读主键不读内容"


class PersonnelError(RuntimeError):
    """人员档案读路径的业务错误（映射为 HTTP 状态码）。"""

    def __init__(self, code: str, message: str, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class PersonnelItem:
    """人员档案的对外视图（已脱敏）。

    形状按简报 §2-C/口径 ⑥ 固定：`id/user_type/display_name/status/last_login_at` + 掩码邮箱。
    **不含 `login_id`**——它是学号/工号，属于「完整查看需权限+理由+审计」那一类标识符，
    而本期响应通道只有 `school.personnel.view`，给出去等于把内部主键扩散给列表调用方。
    """

    id: UUID
    user_type: str
    display_name: str | None
    status: str
    last_login_at: datetime | None
    email_masked: str | None


def _mask_email(email: str | None) -> str | None:
    """邮箱脱敏：保留首字符与域名，中间打码（红线：完整值需权限 + 理由 + 审计）。"""

    if email is None:
        return None
    local, _, domain = email.partition("@")
    if not domain:
        return f"{local[:1]}***"
    return f"{local[:1]}***@{domain}"


def to_item(user: Any) -> PersonnelItem:
    return PersonnelItem(
        id=user.id,
        user_type=user.user_type,
        display_name=user.display_name,
        status=user.status,
        last_login_at=user.last_login_at,
        email_masked=_mask_email(user.email),
    )


def _repository(
    session: AsyncSession, tenant_id: UUID, scope: ScopeDecision
) -> TenantScopedRepository[User]:
    """人员档案仓储：`self` 范围的归属列就是主键（"本人"= 这一行）。"""

    return TenantScopedRepository(session, User, tenant_id, scope=scope, owner_attr="id")


def _require_tenant(principal: Principal) -> UUID:
    """租户主体的 `tenant_id` 必须存在（平台主体不走本模块）。"""

    if principal.realm != "tenant" or principal.tenant_id is None:
        raise PersonnelError("REALM_MISMATCH", "人员档案只在校方租户上下文内可读", 403)
    return principal.tenant_id


async def list_personnel(
    principal: Principal,
    *,
    limit: int = 20,
    offset: int = 0,
    user_type: str | None = None,
) -> list[PersonnelItem]:
    """按数据范围列出人员（看不到的人直接不出现）。

    异常:
        PersonnelError: 主体不是租户主体（403）。
    """

    tenant_id = _require_tenant(principal)
    page_size = max(1, min(limit, MAX_PAGE_SIZE))
    async with db_session(tenant_id=tenant_id) as session:
        repository = _repository(session, tenant_id, principal.scope_context)
        stmt: Select[Any] = (
            repository.base_query()
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(page_size)
            .offset(max(0, offset))
        )
        if user_type is not None:
            stmt = stmt.where(User.user_type == user_type)
        rows = (await session.execute(stmt)).scalars().all()
        return [to_item(row) for row in rows]


async def get_personnel(
    principal: Principal, personnel_id: UUID, *, request_id: str | None = None
) -> PersonnelItem:
    """按 id 读取单个人员档案；越权与不存在都返回 404，越权额外写安全审计。

    异常:
        PersonnelError: 主体不是租户主体（403）；目标不可见或不存在（404）。
    """

    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        repository = _repository(session, tenant_id, principal.scope_context)
        user = await repository.get(personnel_id)
        if user is not None:
            return to_item(user)

    # 走到这里：按当前范围看不到。区分"真不存在"与"存在但越权"，后者要留安全痕迹。
    async with db_session(tenant_id=tenant_id) as session:
        probe = _repository(session, tenant_id, ScopeBypass(reason=_EXISTS_PROBE_REASON))
        exists = await probe.get(personnel_id)
    if exists is not None:
        await _audit_denied(principal, personnel_id, request_id)
    raise PersonnelError("PERSONNEL_NOT_FOUND", "人员不存在或不在可见范围内", 404)


async def _audit_denied(
    principal: Principal, personnel_id: UUID, request_id: str | None
) -> None:
    """写一条 `data_scope.denied` 审计（best-effort：审计故障不改变 404 结果）。"""

    tenant_id = principal.tenant_id
    # 显式收窄而不是 `assert`（bandit 禁止在 app/ 用 assert 当运行时守卫）：`_require_tenant`
    # 已保证非空，走到这里为 None 说明调用方用错了主体 realm，宁可审计不写也不能开无租户会话。
    if tenant_id is None:
        raise PersonnelError("PRINCIPAL_REALM_MISMATCH", "主体缺少租户上下文", 403)
    try:
        async with db_session(tenant_id=tenant_id) as session:
            await append_audit(
                session,
                action="data_scope.denied",
                resource_type="personnel",
                resource_id=str(personnel_id),
                actor_realm="tenant",
                actor_id=principal.user_id,
                tenant_id=tenant_id,
                result="denied",
                metadata={
                    "scope_types": sorted(principal.scope_types),
                    "reason": "out_of_data_scope",
                },
                request_id=request_id,
            )
    except Exception as exc:
        # 与权限拒绝审计同一口径：只记异常类型，不带 traceback（避免 SQL 参数进日志）。
        logging.getLogger(__name__).warning(
            "数据范围拒绝审计写入失败：%s.%s",
            type(exc).__module__,
            type(exc).__name__,
            extra={"personnel_id": str(personnel_id)},
        )


__all__ = [
    "MAX_PAGE_SIZE",
    "PersonnelError",
    "PersonnelItem",
    "get_personnel",
    "list_personnel",
    "to_item",
]
