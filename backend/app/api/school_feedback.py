"""B 端 AI 反馈（M4-AI05，design §5.11）：列表/详情/标记处理/答复。

鉴权：`school.feedback.view`（列表/详情）+ `school.feedback.update`（处理/答复）
——school_admin 默认不含（矩阵 △ 行），校方超管全量型自动获得。
详情快照受控解密（A3）；匿名化反馈不返回快照/comment（A7）；答复落
`ai_feedback_replies`（A5，站内信推送归 M5）。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.principal import Principal, require_permission
from app.feedback.errors import get_kek, http_from
from app.feedback.service import (
    REPLY_MAX_CHARS,
    FeedbackError,
    get_feedback_detail,
    list_feedback,
    mark_handled,
    reply_feedback,
)

router = APIRouter(prefix="/school/feedback", tags=["school-feedback"])

_ViewDep = require_permission("school.feedback.view", realm="tenant")
_UpdateDep = require_permission("school.feedback.update", realm="tenant")


@router.get("")
async def list_school_feedback(
    principal: Annotated[Principal, _ViewDep],
    rating: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> dict[str, object]:
    """反馈列表（rating 可选筛选 + keyset 游标；坏游标 400）。"""

    tenant_id = principal.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="缺少租户上下文")
    try:
        items, next_cursor = await list_feedback(
            tenant_id=tenant_id, rating=rating, limit=limit, cursor=cursor,
        )
    except FeedbackError as exc:
        raise http_from(exc) from exc
    return {"feedback": items, "next_cursor": next_cursor}


@router.get("/{feedback_id}")
async def get_school_feedback(
    feedback_id: UUID,
    principal: Annotated[Principal, _ViewDep],
) -> dict[str, object]:
    """反馈详情：受控解密快照（问题/答案/引用）；匿名化反馈不含快照/comment（A7）。"""

    tenant_id = principal.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="缺少租户上下文")
    try:
        return await get_feedback_detail(
            tenant_id=tenant_id, feedback_id=feedback_id, kek=get_kek(),
        )
    except FeedbackError as exc:
        raise http_from(exc) from exc


@router.post("/{feedback_id}/handle")
async def handle_school_feedback(
    feedback_id: UUID,
    principal: Annotated[Principal, _UpdateDep],
) -> dict[str, object]:
    """标记已处理（A4：submitted→handled；幂等；匿名化反馈 409）。"""

    tenant_id = principal.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="缺少租户上下文")
    try:
        executed_now = await mark_handled(
            tenant_id=tenant_id, handler_id=principal.user_id, feedback_id=feedback_id,
        )
    except FeedbackError as exc:
        raise http_from(exc) from exc
    return {"feedback_id": str(feedback_id), "status": "handled", "executed_now": executed_now}


class FeedbackReplyRequest(BaseModel):
    """答复载荷（≤1000 字；落 ai_feedback_replies）。"""

    content: str = Field(min_length=1, max_length=REPLY_MAX_CHARS)


@router.post("/{feedback_id}/reply")
async def reply_school_feedback(
    feedback_id: UUID,
    payload: FeedbackReplyRequest,
    principal: Annotated[Principal, _UpdateDep],
) -> dict[str, object]:
    """答复反馈（A5）：落表 + 审计；匿名化反馈 409。"""

    tenant_id = principal.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="缺少租户上下文")
    try:
        reply_id = await reply_feedback(
            tenant_id=tenant_id, sender_id=principal.user_id,
            feedback_id=feedback_id, content=payload.content,
        )
    except FeedbackError as exc:
        raise http_from(exc) from exc
    return {"feedback_id": str(feedback_id), "reply_id": str(reply_id)}
