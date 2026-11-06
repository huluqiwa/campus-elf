"""审批引擎最小实现（M3 KB-02 / APV-01 子集，`docs/design-briefs/M3-KB-02.md` 已确认 D6）。

口径（`docs/state-machines.md` §6 / `docs/spec.md` §3.8）：

- 本点只有单步审批：`approval_steps` 恒 1 行，审批人池 = 持 `school.kb.approval`
  且**非提交人非上传者**（`docs/rbac-matrix.md`「上传者不得自批」+ spec「提交人不能自批」；
  判定复用 `app/approval/sod.py::evaluate_self_approval`，例外开关**不接入**，恒 403）；
- 列表/详情/动作按**租户级**（D5：权限码为主判据，不按提交人数据范围裁剪；
  与 `GET /school/permission-accounts` 的 TD-237 口径同源），仓储层用
  `ScopeBypass` 显式声明该口径，租户隔离仍由 `tenant_id` 谓词 + RLS 兜底；
- 决定/撤销走 `with_for_update` 行锁 + PENDING 状态校验：同一审批单只有一个最终动作
  （state-machines §11），并发第二个动作拿到锁后看到终态即 409；
- 审计由调用方（KB 服务层）以业务动作名写（`school.kb.approval.*`），引擎本身不重复留痕；
- 附件表/多步审批/超时提醒/通知不在本点（TD-286 → M5 APV-01 完整版）。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.approval.sod import ApprovalSeparationError, evaluate_self_approval
from app.db.models.approval import (
    APPROVAL_TYPE_KB_DOCUMENT,
    ApprovalAction,
    ApprovalRequest,
    ApprovalStep,
    ApprovalType,
)
from app.db.repository import ScopeBypass, TenantScopedRepository

REASON_MAX_LENGTH = 500

_REPOSITORY_BYPASS = ScopeBypass(
    reason="审批是租户级治理动作（简报 D5）：按权限码鉴权，不按提交人数据范围裁剪"
)


class ApprovalError(Exception):
    """审批业务错误（带机器可读码与建议 HTTP 状态码，供接口层映射）。"""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.extra: dict[str, Any] = extra or {}


@dataclass(frozen=True, slots=True)
class ApprovalStepView:
    id: UUID
    step_no: int
    status: str
    acted_by: UUID | None
    acted_at: datetime | None
    comment: str | None


@dataclass(frozen=True, slots=True)
class ApprovalActionView:
    id: UUID
    actor_id: UUID
    action: str
    reason: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalRequestView:
    id: UUID
    type_code: str
    title: str
    status: str
    result: str | None
    reject_reason: str | None
    submitter_id: UUID
    document_version_id: UUID | None
    payload: dict[str, Any] | None
    current_step: int
    created_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ApprovalDetail:
    request: ApprovalRequestView
    steps: tuple[ApprovalStepView, ...]
    actions: tuple[ApprovalActionView, ...]


def _request_repository(session: AsyncSession, tenant_id: UUID) -> TenantScopedRepository[ApprovalRequest]:
    return TenantScopedRepository(session, ApprovalRequest, tenant_id, scope=_REPOSITORY_BYPASS)


def _step_repository(session: AsyncSession, tenant_id: UUID) -> TenantScopedRepository[ApprovalStep]:
    return TenantScopedRepository(session, ApprovalStep, tenant_id, scope=_REPOSITORY_BYPASS)


def _action_repository(session: AsyncSession, tenant_id: UUID) -> TenantScopedRepository[ApprovalAction]:
    return TenantScopedRepository(session, ApprovalAction, tenant_id, scope=_REPOSITORY_BYPASS)


def _request_view(request: ApprovalRequest, type_code: str) -> ApprovalRequestView:
    return ApprovalRequestView(
        id=request.id,
        type_code=type_code,
        title=request.title,
        status=request.status,
        result=request.result,
        reject_reason=request.reject_reason,
        submitter_id=request.submitter_id,
        document_version_id=request.document_version_id,
        payload=dict(request.payload_json) if request.payload_json else None,
        current_step=request.current_step,
        created_at=request.created_at,
        completed_at=request.completed_at,
    )


def _step_view(step: ApprovalStep) -> ApprovalStepView:
    return ApprovalStepView(
        id=step.id,
        step_no=step.step_no,
        status=step.status,
        acted_by=step.acted_by,
        acted_at=step.acted_at,
        comment=step.comment,
    )


def _action_view(action: ApprovalAction) -> ApprovalActionView:
    return ApprovalActionView(
        id=action.id,
        actor_id=action.actor_id,
        action=action.action,
        reason=action.reason,
        created_at=action.created_at,
    )


async def ensure_type(
    session: AsyncSession, *, tenant_id: UUID, code: str = APPROVAL_TYPE_KB_DOCUMENT
) -> ApprovalType:
    """按租户取审批类型（没有则创建；本点只有 KB_DOCUMENT 一种）。

    首次提交存在并发窗口（同租户两笔同时首提撞 `uq_approval_types_tenant_code`）：
    INSERT 放在 SAVEPOINT 里，撞唯一约束时回滚到保存点后重查（外层事务不受污染）。
    """

    repository: TenantScopedRepository[ApprovalType] = TenantScopedRepository(
        session, ApprovalType, tenant_id, scope=_REPOSITORY_BYPASS
    )
    stmt = repository.base_query().where(ApprovalType.code == code)
    existing = cast(
        ApprovalType | None, (await session.execute(stmt)).scalar_one_or_none()
    )
    if existing is not None:
        return existing
    approval_type = ApprovalType(
        tenant_id=tenant_id,
        code=code,
        name="知识库文档版本审批",
        enabled=True,
    )
    try:
        async with session.begin_nested():
            await repository.add(approval_type)
            await session.flush()
    except IntegrityError:
        recheck = cast(
            ApprovalType | None,
            (await session.execute(stmt)).scalar_one_or_none(),
        )
        if recheck is None:
            raise
        return recheck
    return approval_type


async def submit_request(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    title: str,
    submitter_id: UUID,
    payload: dict[str, Any],
    document_version_id: UUID | None = None,
    type_code: str = APPROVAL_TYPE_KB_DOCUMENT,
) -> ApprovalRequestView:
    """创建审批单（PENDING + 单步 + submit 动作留痕）。

    「同一版本最多一个 PENDING 单」由部分唯一索引兜底：并发撞索引时
    `IntegrityError` 向上抛，由调用方映射为 409。
    """

    approval_type = await ensure_type(session, tenant_id=tenant_id, code=type_code)
    if not approval_type.enabled:
        raise ApprovalError(
            "APPROVAL_TYPE_DISABLED", "该审批类型已停用，不能提交", 409
        )
    request_repository = _request_repository(session, tenant_id)
    step_repository = _step_repository(session, tenant_id)
    action_repository = _action_repository(session, tenant_id)

    request = ApprovalRequest(
        tenant_id=tenant_id,
        type_id=approval_type.id,
        document_version_id=document_version_id,
        submitter_id=submitter_id,
        status="PENDING",
        current_step=1,
        title=title,
        payload_json=payload,
    )
    await request_repository.add(request)
    await session.flush()

    await step_repository.add(
        ApprovalStep(
            tenant_id=tenant_id,
            request_id=request.id,
            step_no=1,
            status="PENDING",
        )
    )
    await action_repository.add(
        ApprovalAction(
            tenant_id=tenant_id,
            request_id=request.id,
            actor_id=submitter_id,
            action="submit",
        )
    )
    await session.flush()
    return _request_view(request, approval_type.code)


async def _locked_request(
    session: AsyncSession, tenant_id: UUID, request_id: UUID
) -> ApprovalRequest | None:
    """行锁取审批单（读改写竞争的唯一收口）。

    必须带 `populate_existing`：调用方往往先普通读过同一行，身份映射里已缓存
    PENDING；`FOR UPDATE` 只保证 SQL 阻塞后拿到**最新行**，不加该选项时
    SQLAlchemy 会把**缓存里的旧属性**原样返回（并发第二路看到假 PENDING → 双赢家）。
    scan_task 没有这个问题是因为它每次都开新短事务（身份映射为空）。
    """

    repository = _request_repository(session, tenant_id)
    stmt = (
        repository.base_query()
        .where(ApprovalRequest.id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return cast(
        ApprovalRequest | None, (await session.execute(stmt)).scalar_one_or_none()
    )


def _guard_self_approval(
    request: ApprovalRequest, actor_id: UUID, forbidden_actor_ids: tuple[UUID, ...]
) -> None:
    """提交人/上传者不能审批（复用 sod 判定内核；例外开关不接入，恒 403）。"""

    for forbidden in forbidden_actor_ids:
        try:
            evaluate_self_approval(
                submitter_id=forbidden,
                reviewer_id=actor_id,
                exception_enabled=False,
                reason=None,
            )
        except ApprovalSeparationError as exc:
            raise ApprovalError(
                exc.code,
                "提交人或上传者不能审批该文档版本（无例外通道）",
                exc.http_status,
            ) from exc


async def decide_request(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    request_id: UUID,
    actor_id: UUID,
    action: str,
    reason: str | None,
    forbidden_actor_ids: tuple[UUID, ...] = (),
) -> ApprovalRequestView:
    """通过（action='approve'）或驳回（action='reject'）一个 PENDING 审批单。

    行锁 + PENDING 校验保证「同一审批单仅一个最终动作」；驳回理由必填。
    业务状态（KB 版本/文档）由调用方在同一事务内迁移。
    """

    if action not in ("approve", "reject"):
        raise ValueError(f"未知的审批动作：{action}")
    request = await _locked_request(session, tenant_id, request_id)
    if request is None:
        raise ApprovalError("APPROVAL_NOT_FOUND", "审批单不存在", 404)
    if request.status != "PENDING":
        raise ApprovalError(
            "APPROVAL_ALREADY_DECIDED",
            "该审批单已有最终结论",
            409,
            extra={"status": request.status, "result": request.result},
        )
    _guard_self_approval(request, actor_id, forbidden_actor_ids)

    cleaned_reason = reason.strip() if reason else None
    if action == "reject" and not cleaned_reason:
        raise ApprovalError("REJECT_REASON_REQUIRED", "驳回必须填写理由", 422)
    if cleaned_reason is not None and len(cleaned_reason) > REASON_MAX_LENGTH:
        raise ApprovalError(
            "REASON_TOO_LONG", f"理由长度不能超过 {REASON_MAX_LENGTH} 字", 422
        )

    now = datetime.now(UTC)
    result = "approve" if action == "approve" else "reject"
    request.status = "APPROVED" if action == "approve" else "REJECTED"
    request.result = result
    request.reject_reason = cleaned_reason if action == "reject" else None
    request.completed_at = now

    step_repository = _step_repository(session, tenant_id)
    step = (
        await session.execute(
            step_repository.base_query().where(
                ApprovalStep.request_id == request_id,
                ApprovalStep.step_no == request.current_step,
            )
        )
    ).scalar_one()
    step.status = request.status
    step.acted_by = actor_id
    step.acted_at = now
    step.comment = cleaned_reason

    await _action_repository(session, tenant_id).add(
        ApprovalAction(
            tenant_id=tenant_id,
            request_id=request.id,
            step_id=step.id,
            actor_id=actor_id,
            action=action,
            reason=cleaned_reason,
        )
    )
    await session.flush()
    return _request_view(request, (await _type_code(session, tenant_id, request.type_id)))


async def cancel_request(
    session: AsyncSession, *, tenant_id: UUID, request_id: UUID, actor_id: UUID
) -> ApprovalRequestView:
    """提交人撤销自己的 PENDING 审批单（版本状态由调用方保持 PENDING_APPROVAL）。"""

    request = await _locked_request(session, tenant_id, request_id)
    if request is None:
        raise ApprovalError("APPROVAL_NOT_FOUND", "审批单不存在", 404)
    if request.status != "PENDING":
        raise ApprovalError(
            "APPROVAL_ALREADY_DECIDED",
            "该审批单已有最终结论，不能撤销",
            409,
            extra={"status": request.status, "result": request.result},
        )
    if request.submitter_id != actor_id:
        raise ApprovalError(
            "APPROVAL_CANCEL_FORBIDDEN", "只有提交人可以撤销该审批单", 403
        )

    now = datetime.now(UTC)
    request.status = "CANCELLED"
    request.result = "cancel"
    request.completed_at = now

    step_repository = _step_repository(session, tenant_id)
    step = (
        await session.execute(
            step_repository.base_query().where(
                ApprovalStep.request_id == request_id,
                ApprovalStep.step_no == request.current_step,
            )
        )
    ).scalar_one()
    step.status = "CANCELLED"
    step.acted_by = actor_id
    step.acted_at = now

    await _action_repository(session, tenant_id).add(
        ApprovalAction(
            tenant_id=tenant_id,
            request_id=request.id,
            step_id=step.id,
            actor_id=actor_id,
            action="cancel",
        )
    )
    await session.flush()
    return _request_view(request, (await _type_code(session, tenant_id, request.type_id)))


async def _type_code(session: AsyncSession, tenant_id: UUID, type_id: UUID) -> str:
    repository: TenantScopedRepository[ApprovalType] = TenantScopedRepository(
        session, ApprovalType, tenant_id, scope=_REPOSITORY_BYPASS
    )
    approval_type = await repository.get(type_id)
    return approval_type.code if approval_type is not None else ""


async def list_todo(
    session: AsyncSession, *, tenant_id: UUID, limit: int = 20, cursor: str | None = None
) -> tuple[list[ApprovalRequestView], str | None]:
    """待办：全部 PENDING 审批单（created_at DESC, id DESC 游标分页）。"""

    repository = _request_repository(session, tenant_id)
    try:
        rows, next_cursor = await repository.list_cursor(
            limit=limit, cursor=cursor, extra=ApprovalRequest.status == "PENDING"
        )
    except ValueError as exc:
        raise ApprovalError("INVALID_CURSOR", "分页游标无效", 400) from exc
    type_codes = await _type_codes_for(session, tenant_id, rows)
    return [_request_view(row, type_codes[row.type_id]) for row in rows], next_cursor


async def list_done(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    limit: int = 20,
    cursor: str | None = None,
) -> tuple[list[ApprovalRequestView], str | None]:
    """已办：与当前用户有过动作往来（提交/通过/驳回/撤销）的审批单。"""

    repository = _request_repository(session, tenant_id)
    action_repository = _action_repository(session, tenant_id)
    # 子查询同样走仓储 base_query（带租户谓词），避免裸 select 触发守卫。
    acted_ids = (
        action_repository.base_query()
        .where(ApprovalAction.actor_id == actor_id)
        .with_only_columns(ApprovalAction.request_id)
    )
    try:
        rows, next_cursor = await repository.list_cursor(
            limit=limit, cursor=cursor, extra=ApprovalRequest.id.in_(acted_ids)
        )
    except ValueError as exc:
        raise ApprovalError("INVALID_CURSOR", "分页游标无效", 400) from exc
    type_codes = await _type_codes_for(session, tenant_id, rows)
    return [_request_view(row, type_codes[row.type_id]) for row in rows], next_cursor


async def _type_codes_for(
    session: AsyncSession, tenant_id: UUID, requests: list[ApprovalRequest]
) -> dict[UUID, str]:
    if not requests:
        return {}
    repository: TenantScopedRepository[ApprovalType] = TenantScopedRepository(
        session, ApprovalType, tenant_id, scope=_REPOSITORY_BYPASS
    )
    rows = (
        await session.execute(
            repository.base_query().where(
                ApprovalType.id.in_({request.type_id for request in requests})
            )
        )
    ).scalars().all()
    return {row.id: row.code for row in rows}


async def get_request_detail(
    session: AsyncSession, *, tenant_id: UUID, request_id: UUID
) -> ApprovalDetail | None:
    """审批单详情（含步骤与动作留痕；跨租/不存在一律 None）。"""

    repository = _request_repository(session, tenant_id)
    request = await repository.get(request_id)
    if request is None:
        return None
    step_repository = _step_repository(session, tenant_id)
    action_repository = _action_repository(session, tenant_id)
    steps = (
        (
            await session.execute(
                step_repository.base_query()
                .where(ApprovalStep.request_id == request_id)
                .order_by(ApprovalStep.step_no.asc())
            )
        )
        .scalars()
        .all()
    )
    actions = (
        (
            await session.execute(
                action_repository.base_query()
                .where(ApprovalAction.request_id == request_id)
                .order_by(ApprovalAction.created_at.asc(), ApprovalAction.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return ApprovalDetail(
        request=_request_view(request, await _type_code(session, tenant_id, request.type_id)),
        steps=tuple(_step_view(step) for step in steps),
        actions=tuple(_action_view(action) for action in actions),
    )
