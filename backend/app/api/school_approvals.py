"""校方审批中心接口（M3 KB-02 / APV-01 最小子集，`docs/design-briefs/M3-KB-02.md` D5/D6）。

- `GET /school/approvals/todo`：待办（全部 PENDING 审批单，游标分页）；
- `GET /school/approvals/done`：已办（与当前用户有过动作往来的审批单）；
- `GET /school/approvals/{request_id}`：详情（含步骤与动作留痕、版本快照 payload）；
- `POST /school/approvals/{request_id}/approve|reject`：通过/驳回（`school.kb.approval`；
  驳回理由必填；提交人/上传者自批恒 403；KB_DOCUMENT 类型才可在此决定）；
- `POST /school/approvals/{request_id}/cancel`：提交人撤销自己的 PENDING 单
  （`school.kb.upload` 自助路径；引擎强校验 submitter_id）。

列表与动作按**租户级**（D5：权限码为主判据，不按提交人数据范围裁剪）。
超时提醒/升级/通知/抄送不在本点（TD-286 → M5）。
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, StringConstraints

from app.api.pagination import PageResponse
from app.approval.service import (
    ApprovalDetail,
    ApprovalError,
    ApprovalRequestView,
    get_request_detail,
    list_done,
    list_todo,
)
from app.auth.principal import Principal, require_permission
from app.db.session import db_session
from app.kb.service import (
    ApprovalOutcome,
    KbError,
    cancel_version_approval,
    decide_version_approval,
)

router = APIRouter(prefix="/school/approvals", tags=["school-approvals"])

TodoDep = require_permission("school.approval.todo", realm="tenant")
KbApproverDep = require_permission("school.kb.approval", realm="tenant")
UploaderDep = require_permission("school.kb.upload", realm="tenant")

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


class ApprovalStepResponse(BaseModel):
    """审批步骤（本点恒 1 步）。"""

    id: UUID
    step_no: int
    status: str
    acted_by: UUID | None
    acted_at: datetime | None
    comment: str | None


class ApprovalActionResponse(BaseModel):
    """审批动作留痕。"""

    id: UUID
    actor_id: UUID
    action: str
    reason: str | None
    created_at: datetime


class ApprovalRequestResponse(BaseModel):
    """审批单（`payload` 为提交时的版本快照）。"""

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


class ApprovalDetailResponse(BaseModel):
    """审批单详情（单 + 步骤 + 动作）。"""

    request: ApprovalRequestResponse
    steps: list[ApprovalStepResponse]
    actions: list[ApprovalActionResponse]


class KbApprovalDecidePayload(BaseModel):
    """通过/驳回的请求体（驳回理由必填由服务层裁决；理由 ≤500 字）。"""

    reason: Annotated[str | None, StringConstraints(max_length=500)] = None


def _tenant_or_403(principal: Principal) -> UUID:
    if principal.realm != "tenant" or principal.tenant_id is None:
        raise HTTPException(status_code=403, detail="审批中心只在校方租户上下文内可用")
    return principal.tenant_id


def _http_error(error: ApprovalError | KbError) -> HTTPException:
    return HTTPException(
        status_code=error.http_status,
        detail={"code": error.code, "message": error.message},
    )


def _request_response(view: ApprovalRequestView) -> ApprovalRequestResponse:
    return ApprovalRequestResponse(
        id=view.id,
        type_code=view.type_code,
        title=view.title,
        status=view.status,
        result=view.result,
        reject_reason=view.reject_reason,
        submitter_id=view.submitter_id,
        document_version_id=view.document_version_id,
        payload=view.payload,
        current_step=view.current_step,
        created_at=view.created_at,
        completed_at=view.completed_at,
    )


def _detail_response(detail: ApprovalDetail) -> ApprovalDetailResponse:
    return ApprovalDetailResponse(
        request=_request_response(detail.request),
        steps=[
            ApprovalStepResponse(
                id=step.id,
                step_no=step.step_no,
                status=step.status,
                acted_by=step.acted_by,
                acted_at=step.acted_at,
                comment=step.comment,
            )
            for step in detail.steps
        ],
        actions=[
            ApprovalActionResponse(
                id=action.id,
                actor_id=action.actor_id,
                action=action.action,
                reason=action.reason,
                created_at=action.created_at,
            )
            for action in detail.actions
        ],
    )


@router.get("/todo", response_model=PageResponse[ApprovalRequestResponse])
async def list_approval_todo(
    viewer: Annotated[Principal, TodoDep],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> PageResponse[ApprovalRequestResponse]:
    """待办：全部 PENDING 审批单（默认 20，上限 100）。"""

    tenant_id = _tenant_or_403(viewer)
    async with db_session(tenant_id=tenant_id) as session:
        try:
            rows, next_cursor = await list_todo(
                session, tenant_id=tenant_id, limit=limit, cursor=cursor
            )
        except ApprovalError as exc:
            raise _http_error(exc) from exc
    return PageResponse(
        items=[_request_response(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.get("/done", response_model=PageResponse[ApprovalRequestResponse])
async def list_approval_done(
    viewer: Annotated[Principal, TodoDep],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> PageResponse[ApprovalRequestResponse]:
    """已办：与当前用户有过动作往来的审批单。"""

    tenant_id = _tenant_or_403(viewer)
    async with db_session(tenant_id=tenant_id) as session:
        try:
            rows, next_cursor = await list_done(
                session, tenant_id=tenant_id, actor_id=viewer.user_id, limit=limit, cursor=cursor
            )
        except ApprovalError as exc:
            raise _http_error(exc) from exc
    return PageResponse(
        items=[_request_response(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.get("/{approval_request_id}", response_model=ApprovalDetailResponse)
async def get_approval_detail(
    approval_request_id: UUID,
    viewer: Annotated[Principal, TodoDep],
) -> ApprovalDetailResponse:
    """审批单详情（跨租/不存在一律 404）。"""

    tenant_id = _tenant_or_403(viewer)
    async with db_session(tenant_id=tenant_id) as session:
        detail = await get_request_detail(
            session, tenant_id=tenant_id, request_id=approval_request_id
        )
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "APPROVAL_NOT_FOUND", "message": "审批单不存在"},
        )
    return _detail_response(detail)


@router.post("/{approval_request_id}/approve", response_model=ApprovalRequestResponse)
async def approve_kb_approval(
    approval_request_id: UUID,
    approver: Annotated[Principal, KbApproverDep],
    request: Request,
    payload: KbApprovalDecidePayload | None = None,
) -> ApprovalRequestResponse:
    """通过一个 KB_DOCUMENT 审批单（提交人/上传者自批恒 403）。"""

    tenant_id = _tenant_or_403(approver)
    try:
        outcome = await decide_version_approval(
            tenant_id=tenant_id,
            approval_request_id=approval_request_id,
            actor_id=approver.user_id,
            action="approve",
            reason=payload.reason if payload is not None else None,
            request_id=getattr(request.state, "request_id", None),
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return _outcome_request(outcome)


@router.post("/{approval_request_id}/reject", response_model=ApprovalRequestResponse)
async def reject_kb_approval(
    approval_request_id: UUID,
    approver: Annotated[Principal, KbApproverDep],
    request: Request,
    payload: KbApprovalDecidePayload | None = None,
) -> ApprovalRequestResponse:
    """驳回一个 KB_DOCUMENT 审批单（理由必填；理由随审批单与审计留痕）。"""

    tenant_id = _tenant_or_403(approver)
    try:
        outcome = await decide_version_approval(
            tenant_id=tenant_id,
            approval_request_id=approval_request_id,
            actor_id=approver.user_id,
            action="reject",
            reason=payload.reason if payload is not None else None,
            request_id=getattr(request.state, "request_id", None),
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return _outcome_request(outcome)


@router.post("/{approval_request_id}/cancel", response_model=ApprovalRequestResponse)
async def cancel_kb_approval(
    approval_request_id: UUID,
    submitter: Annotated[Principal, UploaderDep],
    request: Request,
) -> ApprovalRequestResponse:
    """提交人撤销自己的 PENDING 审批单（版本保持 PENDING_APPROVAL 可再提交）。"""

    tenant_id = _tenant_or_403(submitter)
    try:
        view = await cancel_version_approval(
            tenant_id=tenant_id,
            approval_request_id=approval_request_id,
            actor_id=submitter.user_id,
            request_id=getattr(request.state, "request_id", None),
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return _request_response(view)


def _outcome_request(outcome: ApprovalOutcome) -> ApprovalRequestResponse:
    return _request_response(outcome.request)
