"""平台侧租户接口（M1-B）。

鉴权说明：M1-C 之前没有正式登录，审核接口使用**开发/测试专用**静态令牌：
- `DEV_PLATFORM_REVIEW_TOKEN` 未配置 → 403（生产由配置校验禁止配置该令牌）；
- 配置后要求 `Authorization: Bearer <token>`；
- M1-C 用 JWT + `platform.tenant.review` 权限替换本依赖并删除令牌（TD）。
"""

import secrets
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.audit.writer import append_audit
from app.config import get_settings
from app.db.session import db_session
from app.onboarding import service
from app.onboarding.service import OnboardingError

router = APIRouter(prefix="/platform", tags=["platform"])


async def require_platform_reviewer(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> UUID:
    """校验开发令牌并返回审核人占位 id（M1-C 改为 JWT 主体）。

    - 常量时间比较，避免令牌被计时侧信道枚举；
    - 未配置令牌 → 403；缺失/错误 → 401，并写一条拒绝审计。

    异常:
        HTTPException: 未启用（403）、令牌缺失/错误（401）。
    """

    settings = get_settings()
    expected = settings.dev_platform_review_token
    if expected is None or not expected.get_secret_value():
        raise HTTPException(status_code=403, detail="审核接口未启用（等待正式登录）")
    provided = authorization if authorization is not None else ""
    expected_header = f"Bearer {expected.get_secret_value()}"
    if not secrets.compare_digest(provided.encode(), expected_header.encode()):
        async with db_session(platform=True) as session:
            await append_audit(
                session,
                action="platform.review.denied",
                resource_type="platform_review",
                actor_realm="platform",
                result="failure",
                metadata={"path": request.url.path},
                request_id=getattr(request.state, "request_id", None),
            )
        raise HTTPException(status_code=401, detail="审核令牌无效")
    return service.SYSTEM_REVIEWER_ID


ReviewerId = Annotated[UUID, Depends(require_platform_reviewer)]


class RegistrationResponse(BaseModel):
    """注册提交结果。"""

    request_id: UUID
    status: Literal["pending", "approved"]
    school_name_duplicate_hint: bool
    email_sent: bool = False
    message: str


class ReviewPayload(BaseModel):
    """审核请求。"""

    result: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=500)


class ReviewResponse(BaseModel):
    """审核结果。"""

    status: Literal["approved", "rejected"]
    tenant_id: UUID | None = None
    school_code: str | None = None
    login_id: str | None = None
    email_sent: bool = False


class TenantItem(BaseModel):
    """租户列表项。"""

    id: UUID
    name: str
    school_code: str | None
    status: str
    email: str | None
    activated_at: str | None


class RequestItem(BaseModel):
    """注册申请列表项。"""

    id: UUID
    school_name: str
    email: str
    status: str
    school_name_duplicate_hint: bool
    submitted_at: str
    reject_reason: str | None


class ReviewRecordItem(BaseModel):
    """审核记录项。"""

    id: UUID
    request_id: UUID
    tenant_id: UUID | None
    reviewer_id: UUID
    result: str
    reason: str | None
    snapshot_object_key: str | None
    reviewed_at: str


def _http_error(exc: OnboardingError) -> HTTPException:
    """把业务错误映射为 HTTP 错误（错误信封由全局处理器统一）。"""

    return HTTPException(status_code=exc.http_status, detail=exc.message)


@router.post("/tenants/register", response_model=RegistrationResponse, status_code=201)
async def register_tenant(
    request: Request,
    school_name: Annotated[str, Form(min_length=1, max_length=200)],
    address: Annotated[str, Form(min_length=1, max_length=300)],
    leadership: Annotated[str, Form(min_length=1, max_length=200)],
    email: Annotated[str, Form(min_length=5, max_length=254)],
    qualification: Annotated[UploadFile, File()],
) -> RegistrationResponse:
    """学校提交注册（公开接口；资质凭证 PDF/PNG/JPG，≤10MB）。

    异常:
        HTTPException: 字段/文件非法（422/413）或邮箱重复（409）。
    """

    # 读取上限 +1 字节，防止超大文件占满内存
    raw = await qualification.read(service.MAX_QUALIFICATION_BYTES + 1)
    try:
        outcome = await service.submit_registration(
            school_name=school_name,
            address=address,
            leadership=leadership,
            email=email,
            filename=qualification.filename or "",
            content_type=qualification.content_type,
            data=raw,
            request_id=getattr(request.state, "request_id", None),
        )
    except OnboardingError as exc:
        raise _http_error(exc) from exc
    return RegistrationResponse(
        request_id=outcome.request_id,
        status=outcome.status,
        school_name_duplicate_hint=outcome.school_name_duplicate_hint,
        email_sent=outcome.email_sent,
        message="注册申请已提交，等待平台审核"
        if outcome.status == "pending"
        else "注册已自动开通（开发/测试模式）",
    )


@router.post("/tenant-requests/{request_id}/review", response_model=ReviewResponse)
async def review_tenant_request(
    request_id: UUID,
    payload: ReviewPayload,
    request: Request,
    reviewer_id: ReviewerId,
) -> ReviewResponse:
    """审核注册申请（通过/拒绝；通过后开通租户并发初始密码邮件）。

    异常:
        HTTPException: 申请不存在（404）、已审核（409）、拒绝缺理由（422）。
    """

    try:
        if payload.result == "approved":
            outcome = await service.approve_request(
                request_id,
                reviewer_id=reviewer_id,
                reason=payload.reason,
                request_id=getattr(request.state, "request_id", None),
            )
            return ReviewResponse(
                status="approved",
                tenant_id=outcome.tenant_id,
                school_code=outcome.school_code,
                login_id=outcome.login_id,
                email_sent=outcome.email_sent,
            )
        if payload.reason is None:
            raise OnboardingError("REASON_REQUIRED", "拒绝必须填写理由", 422)
        await service.reject_request(
            request_id,
            reviewer_id=reviewer_id,
            reason=payload.reason,
            request_id=getattr(request.state, "request_id", None),
        )
        return ReviewResponse(status="rejected")
    except OnboardingError as exc:
        raise _http_error(exc) from exc


@router.get("/tenants", response_model=list[TenantItem])
async def list_tenants(
    reviewer_id: ReviewerId, status: str | None = None
) -> list[TenantItem]:
    """列出学校（平台侧）。"""

    rows = await service.list_tenants(status=status)
    return [
        TenantItem(
            id=row.id,
            name=row.name,
            school_code=row.school_code,
            status=row.status,
            email=row.email,
            activated_at=row.activated_at.isoformat() if row.activated_at else None,
        )
        for row in rows
    ]


@router.get("/tenant-requests", response_model=list[RequestItem])
async def list_tenant_requests(
    reviewer_id: ReviewerId, status: str | None = None
) -> list[RequestItem]:
    """列出注册申请（平台侧，可按状态过滤）。"""

    rows = await service.list_registration_requests(status=status)
    return [
        RequestItem(
            id=row.id,
            school_name=row.school_name,
            email=row.email,
            status=row.status,
            school_name_duplicate_hint=row.school_name_duplicate_hint,
            submitted_at=row.submitted_at.isoformat(),
            reject_reason=row.reject_reason,
        )
        for row in rows
    ]


@router.get("/tenants/{tenant_id}/review-records", response_model=list[ReviewRecordItem])
async def list_review_records(tenant_id: UUID, reviewer_id: ReviewerId) -> list[ReviewRecordItem]:
    """某学校的审核记录（平台侧）。"""

    rows = await service.list_review_records(tenant_id)
    return [
        ReviewRecordItem(
            id=row.id,
            request_id=row.request_id,
            tenant_id=row.tenant_id,
            reviewer_id=row.reviewer_id,
            result=row.result,
            reason=row.reason,
            snapshot_object_key=row.snapshot_object_key,
            reviewed_at=row.reviewed_at.isoformat(),
        )
        for row in rows
    ]
