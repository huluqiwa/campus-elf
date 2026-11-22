"""C 端 AI 反馈（M4-AI05）：提交（幂等重放）+ 自助删除（匿名化）。

提交即知情同意（consent_at 服务端记录，A1）；快照 AES-GCM 加密 + comment
PII 脱敏（A2，D2/D3）；自助删除立即匿名化（A6——M5 账号删除流程复用同一函数）。
鉴权：`campus.feedback.create`（student/teacher C 端默认持有——rbac 目录）。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth.principal import Principal, require_permission
from app.feedback.errors import get_kek, http_from
from app.feedback.service import (
    COMMENT_MAX_CHARS,
    FeedbackError,
    anonymize_feedback,
    submit_feedback,
)

router = APIRouter(prefix="/campus", tags=["campus"])

_CreateDep = require_permission("campus.feedback.create", realm="tenant")


class FeedbackCreateRequest(BaseModel):
    """提交载荷（卡 5：rating 词表 + tags 枚举 + comment ≤500——服务层再校验）。"""

    conversation_id: UUID
    turn_id: UUID
    trace_id: str = Field(min_length=1, max_length=64)
    rating: str = Field(min_length=1, max_length=16)
    reason_tags: list[str] = Field(default_factory=list, max_length=10)
    comment: str | None = Field(default=None, max_length=COMMENT_MAX_CHARS)


@router.post("/feedback")
async def create_feedback(
    request: Request,
    payload: FeedbackCreateRequest,
    principal: Annotated[Principal, _CreateDep],
) -> JSONResponse:
    """提交反馈：201 新建 / 200 幂等重放（replayed=true，卡 2）。"""

    tenant_id = principal.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="缺少租户上下文")
    try:
        feedback_id, replayed = await submit_feedback(
            tenant_id=tenant_id,
            user_id=principal.user_id,
            conversation_id=payload.conversation_id,
            turn_id=payload.turn_id,
            trace_id=payload.trace_id,
            rating=payload.rating,
            reason_tags=payload.reason_tags,
            comment=payload.comment,
            kek=get_kek(),
        )
    except FeedbackError as exc:
        raise http_from(exc) from exc
    return JSONResponse(
        status_code=(200 if replayed else 201),
        content={"feedback_id": str(feedback_id), "replayed": replayed},
    )


@router.delete("/feedback/{feedback_id}")
async def delete_feedback(
    feedback_id: UUID,
    principal: Annotated[Principal, _CreateDep],
) -> dict[str, object]:
    """自助删除反馈（A6）：立即匿名化。

    身份断链语义（§10.2.3）：匿名化后 user_id 已置空，任何人（含原本人）再删
    一律 404（不泄露存在性）；幂等体现在效果层面（无可再清）。
    """

    tenant_id = principal.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="缺少租户上下文")
    try:
        await anonymize_feedback(
            tenant_id=tenant_id, user_id=principal.user_id, feedback_id=feedback_id,
        )
    except FeedbackError as exc:
        raise http_from(exc) from exc
    return {"feedback_id": str(feedback_id), "anonymized": True}
