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
from app.config import get_settings
from app.feedback.service import (
    COMMENT_MAX_CHARS,
    FeedbackError,
    anonymize_feedback,
    submit_feedback,
)
from app.security.encryption import decode_kek

router = APIRouter(prefix="/campus", tags=["campus"])

_CreateDep = require_permission("campus.feedback.create", realm="tenant")

_ERROR_STATUS = {
    "FEEDBACK_TARGET_NOT_FOUND": 404,
    "FEEDBACK_RATING_INVALID": 422,
    "FEEDBACK_TAGS_INVALID": 422,
    "FEEDBACK_COMMENT_TOO_LONG": 422,
    "FEEDBACK_REPLY_INVALID": 422,
    "FEEDBACK_ANONYMIZED": 409,
    "FEEDBACK_CURSOR_INVALID": 400,
}


def _http_from(exc: FeedbackError) -> HTTPException:
    """领域错误码 → HTTP 状态（端点层统一映射）。"""

    return HTTPException(status_code=_ERROR_STATUS.get(exc.code, 400), detail=exc.code)


def _kek() -> bytes:
    """ENCRYPTION_KEK（复用既有主密钥，AAD 域隔离——简报 D2）。"""

    return decode_kek(get_settings().encryption_kek)


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
            kek=_kek(),
        )
    except FeedbackError as exc:
        raise _http_from(exc) from exc
    return JSONResponse(
        status_code=(200 if replayed else 201),
        content={"feedback_id": str(feedback_id), "replayed": replayed},
    )


@router.delete("/feedback/{feedback_id}")
async def delete_feedback(
    feedback_id: UUID,
    principal: Annotated[Principal, _CreateDep],
) -> dict[str, object]:
    """自助删除反馈（A6）：立即匿名化；幂等（已匿名化再删仍 200）。"""

    tenant_id = principal.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="缺少租户上下文")
    try:
        await anonymize_feedback(
            tenant_id=tenant_id, user_id=principal.user_id, feedback_id=feedback_id,
        )
    except FeedbackError as exc:
        raise _http_from(exc) from exc
    return {"feedback_id": str(feedback_id), "anonymized": True}
