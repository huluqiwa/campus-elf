"""平台跨租**只读**最小通道（M1-E2 口径 ②，TD-072）。

平台连接读不到业务表（RLS：`campus_elf_platform` 对 `users` 等表无读权限），
所以跨租读取必须由代码显式开一个**目标租户**的租户连接会话。这条通道因此天然高危，
本期把它收口成三件事，缺一即拒绝：

1. **权限**：`platform.cross_tenant.read`（矩阵 §4 出厂默认只给平台超管，运营与审计角色均为 —，需要时显式授予）；
2. **只读**：事务第一条语句就是 `SET LOCAL TRANSACTION READ ONLY`，任何写入在数据库层被拒；
3. **必填理由 + 强制审计**：理由随请求写进 `audit_logs`，事后能回答"谁、看哪个学校、为什么看"。

一次只允许一个 `tenant_id`（不是列表参数）：批量跨租读取会把"看了多少学校"稀释成一条审计，
本期不做（TD-153）。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import append_audit
from app.auth.principal import Principal
from app.db.models.identity import User
from app.db.repository import ScopeBypass, TenantScopedRepository
from app.db.session import get_session_factory
from app.personnel.service import MAX_PAGE_SIZE, PersonnelItem, to_item

# 跨租读取不按范围过滤的理由：平台主体没有 data_scopes（矩阵 §5-11），
# 这条通道的收口靠权限 + 只读事务 + 理由 + 审计，而不是租户侧的 self/school 范围。
CROSS_TENANT_BYPASS_REASON = (
    "平台跨租只读通道：可见性由 platform.cross_tenant.read 与只读事务收口，"
    "平台主体本身不参与租户侧数据范围"
)

MAX_REASON_LENGTH = 500


class CrossTenantError(RuntimeError):
    """跨租只读通道的业务错误。"""

    def __init__(self, code: str, message: str, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class CrossTenantReadResult:
    """一次跨租读取的结果（回显目标租户，便于调用方与审计对齐）。"""

    tenant_id: UUID
    items: list[PersonnelItem]


@asynccontextmanager
async def _read_only_tenant_session(tenant_id: UUID) -> AsyncIterator[AsyncSession]:
    """目标租户的**只读**会话：先声明只读，再注入 `app.tenant_id`。

    顺序不能颠倒：`SET TRANSACTION` 只允许出现在事务的第一条语句位置，而
    `select set_config(...)` 已经算一条查询。写反了 PG 会直接报错，
    所以"只读"这一条不会被静默丢掉。
    """

    factory = get_session_factory(platform=False)
    async with factory() as session, session.begin():
        await session.execute(text("set local transaction read only"))
        await session.execute(
            text("select set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        yield session


@asynccontextmanager
async def _platform_session() -> AsyncIterator[AsyncSession]:
    """平台连接会话（读 `tenants` 与写审计用；它读不到业务表，正是本模块存在的原因）。"""

    factory = get_session_factory(platform=True)
    async with factory() as session, session.begin():
        await session.execute(text("select set_config('app.platform_context', 'on', true)"))
        yield session


def _require_platform(principal: Principal) -> None:
    """ realm 校验：本通道只对平台主体开放（租户主体的跨租读取没有合法语义）。"""

    if principal.realm != "platform":
        raise CrossTenantError("REALM_MISMATCH", "跨租只读通道只对平台主体开放", 403)


async def _tenant_exists(tenant_id: UUID) -> bool:
    """目标租户是否存在（**只看主键，不看 `status`**）。

    口径：`suspended`/`deleted` 的学校仍允许平台只读取证 —— 停服与删除都是运营动作，
    而跨租通道的典型需求方恰恰是"出问题时来看一眼"的事后场景（审核、纠纷、迁移核对）。
    因此这里不做状态探测的遮蔽（平台侧本来就能从 `platform.tenant.view` 读到状态，遮蔽没有意义）；
    不存在 → 404。要不要把"已删除租户"排除掉，留待有真实需求时决定（TD-153 同一条通道上讨论）。
    """

    async with _platform_session() as session:
        found = await session.scalar(
            text("select 1 from tenants where id = :tid"), {"tid": str(tenant_id)}
        )
    return found is not None


async def read_tenant_personnel(
    principal: Principal,
    *,
    tenant_id: UUID,
    reason: str | None,
    limit: int = 20,
    offset: int = 0,
    request_id: str | None = None,
) -> CrossTenantReadResult:
    """跨租读取一个学校的人员档案（只读、必填理由、强制审计）。

    异常:
        CrossTenantError：主体不是平台主体（403）、理由缺失或过长（422）、
            目标租户不存在（404）。
    """

    _require_platform(principal)
    normalized_reason = (reason or "").strip()
    if not normalized_reason:
        raise CrossTenantError("REASON_REQUIRED", "跨租读取必须填写理由", 422)
    if len(normalized_reason) > MAX_REASON_LENGTH:
        raise CrossTenantError(
            "REASON_TOO_LONG", f"跨租读取理由不得超过 {MAX_REASON_LENGTH} 字", 422
        )

    if not await _tenant_exists(tenant_id):
        raise CrossTenantError("TENANT_NOT_FOUND", "目标学校不存在", 404)

    page_size = max(1, min(limit, MAX_PAGE_SIZE))
    async with _read_only_tenant_session(tenant_id) as session:
        repository: TenantScopedRepository[User] = TenantScopedRepository(
            session,
            User,
            tenant_id,
            scope=ScopeBypass(reason=CROSS_TENANT_BYPASS_REASON),
            owner_attr="id",
        )
        rows = await repository.list_all(limit=page_size, offset=max(0, offset))

    items = [to_item(row) for row in rows]
    await _audit_read(principal, tenant_id, normalized_reason, len(items), request_id)
    return CrossTenantReadResult(tenant_id=tenant_id, items=items)


async def _audit_read(
    principal: Principal,
    tenant_id: UUID,
    reason: str,
    row_count: int,
    request_id: str | None,
) -> None:
    """记录一次成功的跨租读取。

    这里**不吞异常**（与人员越权审计相反）：没有留下审计痕迹的跨租读取属于不可审计的访问，
    宁可让请求失败，也不能让"看了哪个学校"这件事静默发生。
    """

    async with _platform_session() as session:
        await append_audit(
            session,
            action="platform.cross_tenant.read",
            resource_type="tenant",
            resource_id=str(tenant_id),
            actor_realm="platform",
            actor_id=principal.user_id,
            result="success",
            metadata={"reason": reason, "row_count": row_count, "fields": "masked"},
            request_id=request_id,
        )


__all__ = [
    "CROSS_TENANT_BYPASS_REASON",
    "MAX_REASON_LENGTH",
    "CrossTenantError",
    "CrossTenantReadResult",
    "read_tenant_personnel",
]
