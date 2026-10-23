"""学校注册与审核服务（TEN-01/02/03）。

业务约束（`docs/design-briefs/M1-B-tenant-onboarding.md`）：
- 邮箱唯一（仅未拒绝的申请占用），校名重复只打 `duplicate_hint`；
- 审核幂等：数据库函数 `review_registration_request()` 内 `SELECT ... FOR UPDATE`，
  同一申请只能审一次；状态机与审核记录由触发器/唯一约束兜底；
- 通过时在一个 SECURITY DEFINER 函数内原子完成：创建租户（active + 学校编号）、
  3 个内置角色、超管账号与角色绑定、审核记录、审计、状态迁移；
  平台运行角色没有这些表的写权限，只能 EXECUTE 该函数；
- 邮件在事务内发送：失败抛 502 并回滚整笔开通（申请保持 pending），
  反向窗口（邮件已发、提交失败）仅导致"邮件里的密码无对应账号"，见 TD-088；
- 生产禁止 `DEV_SKIP_SCHOOL_REVIEW`（配置校验已强制）。
"""

import logging
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, NoReturn
from uuid import UUID, uuid4

import anyio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.approval.sod import ApprovalSeparationError, evaluate_self_approval
from app.audit.redact import mask_email, truncate_text
from app.audit.writer import append_audit
from app.auth.passwords import generate_initial_password, hash_password
from app.config import get_settings
from app.db.models.onboarding import SchoolReviewRecord, TenantRegistrationRequest
from app.db.models.tenant import Tenant
from app.db.session import db_session
from app.integrations.mail import build_mailer, render_tenant_approved_email
from app.settings.store import self_exception_enabled
from app.storage.objects import ObjectStore, build_object_store

SYSTEM_REVIEWER_ID = UUID("00000000-0000-0000-0000-0000000000aa")
SYSTEM_ACTOR_REALM = "system"

MAX_QUALIFICATION_BYTES = 10 * 1024 * 1024
MAX_TEXT_FIELD = 200
MAX_ADDRESS = 300
MAX_REASON = 500
MAX_EMAIL = 254

ALLOWED_MIME: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
}

# 严格邮箱校验：单一 @、无空白/控制字符、local ≤64 且点号不连续/不首尾、总长 ≤254。
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

_MAGIC_PREFIXES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
}

ReviewResult = Literal["approved", "rejected"]


class OnboardingError(Exception):
    """业务错误（带 HTTP 状态码与错误码，供接口层映射）。"""

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        """初始化错误。

        参数:
            code: 机器可读错误码。
            message: 面向用户的中文说明。
            http_status: 建议的 HTTP 状态码。
        """

        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class _EmailDeliveryFailed(Exception):
    """内部信号：事务内邮件发送失败（触发回滚后补写失败审计）。"""


@dataclass(frozen=True, slots=True)
class RegistrationOutcome:
    """注册结果。"""

    request_id: UUID
    status: str
    school_name_duplicate_hint: bool
    email_sent: bool = False


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """审核通过结果。"""

    tenant_id: UUID
    school_code: str
    login_id: str
    email_sent: bool


def _has_control_chars(value: str) -> bool:
    """是否包含 C0/C1 控制字符（防 CRLF 头注入与邮件构造失败）。"""

    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def normalize_email(raw: str) -> str:
    """规范化并严格校验邮箱（去空白、小写）。

    异常:
        OnboardingError: 非法邮箱（422）。
    """

    email = raw.strip().lower()
    if (
        len(email) < 6
        or len(email) > MAX_EMAIL
        or any(ch in email for ch in ("\r", "\n", "\t", " "))
        or _has_control_chars(email)
        or email.count("@") != 1
        or len(email.split("@", 1)[0]) > 64
        or _EMAIL_RE.fullmatch(email) is None
    ):
        raise OnboardingError("INVALID_EMAIL", "邮箱格式不正确", 422)
    return email


def validate_qualification(content_type: str | None, filename: str, data: bytes) -> str:
    """校验资质文件并返回后缀。

    异常:
        OnboardingError: 类型不支持 / 空文件 / 超出大小限制 / 魔数与声明不符。
    """

    if content_type not in ALLOWED_MIME:
        raise OnboardingError("INVALID_FILE_TYPE", "资质凭证仅支持 PDF/PNG/JPG", 422)
    if len(data) == 0:
        raise OnboardingError("EMPTY_FILE", "资质凭证为空", 422)
    if len(data) > MAX_QUALIFICATION_BYTES:
        raise OnboardingError("FILE_TOO_LARGE", "资质凭证不能超过 10MB", 413)
    signatures = _MAGIC_PREFIXES[content_type]
    if not any(data.startswith(signature) for signature in signatures):
        raise OnboardingError(
            "FILE_CONTENT_MISMATCH", "资质凭证内容与声明类型不符", 422
        )
    # 以 MIME 为准决定存储后缀（扩展名不可信）
    _ = filename
    return ALLOWED_MIME[content_type]


def _validate_text(raw: str, *, field: str, maximum: int = MAX_TEXT_FIELD) -> str:
    """校验文本字段非空、不超长且不含控制字符。"""

    value = raw.strip()
    if not value or len(value) > maximum:
        raise OnboardingError(
            "INVALID_FIELD", f"{field} 不能为空且不超过 {maximum} 字", 422
        )
    if _has_control_chars(value):
        raise OnboardingError("INVALID_FIELD", f"{field} 不能包含控制字符", 422)
    return value


async def _school_name_duplicate(session: AsyncSession, school_name: str) -> bool:
    """校名是否已出现在现有租户或未拒绝的申请中（仅提示，不阻断）。"""

    normalized = school_name.strip()
    tenant_hit = await session.scalar(
        select(Tenant.id).where(Tenant.name == normalized).limit(1)
    )
    if tenant_hit is not None:
        return True
    request_hit = await session.scalar(
        select(TenantRegistrationRequest.id)
        .where(TenantRegistrationRequest.school_name == normalized)
        .where(TenantRegistrationRequest.status.in_(["pending", "approved"]))
        .limit(1)
    )
    return request_hit is not None


async def _unique_school_code(session: AsyncSession) -> str:
    """生成唯一学校编号（`SCH-` + 8 位随机；唯一约束兜底）。

    异常:
        OnboardingError: 连续冲突（概率极低）。
    """

    from app.onboarding.codes import generate_school_code

    for _ in range(8):
        code = generate_school_code()
        exists = await session.scalar(select(Tenant.id).where(Tenant.school_code == code).limit(1))
        if exists is None:
            return code
    raise OnboardingError("CODE_EXHAUSTED", "学校编号生成失败，请重试", 503)


async def submit_registration(
    *,
    school_name: str,
    address: str,
    leadership: str,
    email: str,
    filename: str,
    content_type: str | None,
    data: bytes,
    request_id: str | None = None,
    store: ObjectStore | None = None,
) -> RegistrationOutcome:
    """提交学校注册申请（去重 → 凭证存储 → 落库 → 审计）。

    先做去重再写对象，失败时删除已写对象，避免重复提交产生孤儿文件。

    异常:
        OnboardingError: 字段/文件非法，或邮箱已有进行中的申请（409）。
    """

    clean_name = _validate_text(school_name, field="school_name")
    clean_address = _validate_text(address, field="address", maximum=MAX_ADDRESS)
    clean_leadership = _validate_text(leadership, field="leadership")
    normalized_email = normalize_email(email)
    suffix = validate_qualification(content_type, filename, data)
    settings = get_settings()
    object_store = store or build_object_store(settings)

    registration_id = uuid4()
    object_key = f"onboarding/{registration_id}/qualification{suffix}"
    digest = sha256(data).hexdigest()

    hint = False
    try:
        async with db_session(platform=True) as session:
            hint = await _school_name_duplicate(session, clean_name)
            # DB 去重通过后再写对象；后续任何失败（含提交时唯一约束冲突）都清理
            await object_store.put(object_key, data, content_type or "application/octet-stream")
            payload = await session.scalar(
                text(
                    "select submit_tenant_registration("
                    ":id, :school_name, :address, :leadership, :email, :object_key, "
                    ":sha256, :size, :mime, :hint, :http_request_id)"
                ),
                {
                    "id": registration_id,
                    "school_name": clean_name,
                    "address": clean_address,
                    "leadership": clean_leadership,
                    "email": normalized_email,
                    "object_key": object_key,
                    "sha256": digest,
                    "size": len(data),
                    "mime": content_type or "",
                    "hint": hint,
                    "http_request_id": request_id,
                },
            )
            if isinstance(payload, dict) and payload.get("error") == "DUPLICATE_EMAIL":
                raise OnboardingError(
                    "DUPLICATE_EMAIL", "该邮箱已有进行中或已开通的申请，请勿重复提交", 409
                )
    except IntegrityError as exc:  # 并发下同一邮箱的最终防线（部分唯一索引）
        await object_store.delete(object_key)
        raise OnboardingError("DUPLICATE_EMAIL", "该邮箱已有进行中的申请", 409) from exc
    except BaseException:
        await object_store.delete(object_key)
        raise

    if settings.dev_skip_school_review:
        # 开发/测试专用：直接开通（生产由配置校验禁止）
        outcome = await approve_request(
            registration_id,
            reviewer_id=SYSTEM_REVIEWER_ID,
            reason="DEV_SKIP_SCHOOL_REVIEW",
            request_id=request_id,
            store=object_store,
        )
        return RegistrationOutcome(
            request_id=registration_id,
            status="approved",
            school_name_duplicate_hint=hint,
            email_sent=outcome.email_sent,
        )
    return RegistrationOutcome(
        request_id=registration_id,
        status="pending",
        school_name_duplicate_hint=hint,
    )


_REVIEW_ERRORS: dict[str, tuple[int, str]] = {
    "REQUEST_NOT_FOUND": (404, "注册申请不存在"),
    "ALREADY_REVIEWED": (409, "该申请已被审核"),
    "REASON_REQUIRED": (422, "拒绝必须填写理由"),
    "INVALID_RESULT": (500, "审核结果非法"),
    "INVALID_PAYLOAD": (500, "开通参数不完整"),
    # 数据库函数里的第二道判定：正常路径由接口层先挡住（拿到干净的 403/422），
    # 这里兜的是"应用层读到的例外开关与函数读到的不是同一时刻"（运营并发改开关）。
    "SELF_APPROVAL_FORBIDDEN": (403, "提交人不能审批自己提交的事项"),
    "SELF_APPROVAL_REASON_REQUIRED": (422, "使用自批例外必须填写理由"),
}


class _SelfApprovalDenied(Exception):
    """分权判定不通过（内部信号：先让本事务整体回滚，再由调用方补写拒绝审计）。"""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        http_status: int,
        registration_id: UUID,
        reviewer_id: UUID,
        request_id: str | None,
    ) -> None:
        """记录错误码、面向用户的说明与建议 HTTP 状态码，以及审计所需的上下文。"""

        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.registration_id = registration_id
        self.reviewer_id = reviewer_id
        self.request_id = request_id


async def _guard_self_approval(
    session: AsyncSession,
    *,
    registration: TenantRegistrationRequest,
    reviewer_id: UUID,
    reason: str | None,
    request_id: str | None,
) -> None:
    """RBAC-03 第一道判定（口径 ③：应用层 + 数据库约束，不加触发器）。

    开关**每请求从库读**（口径 ⑧）：运营在平台接口改完，下一次审批即用新值，
    不重启、不踢在线会话。用掉例外时在同一个事务里写 `approval.self_exception.used`
    （例外必须留下"谁批的、为什么批"的证据，否则开关等于没有审计）。

    异常:
        _SelfApprovalDenied：同人审批且例外未开启（403）或缺理由（422）。
    """

    try:
        outcome = evaluate_self_approval(
            submitter_id=registration.submitted_by_platform_user_id,
            reviewer_id=reviewer_id,
            exception_enabled=await self_exception_enabled(session),
            reason=reason,
        )
    except ApprovalSeparationError as exc:
        raise _SelfApprovalDenied(
            code=exc.code,
            message=exc.message,
            http_status=exc.http_status,
            registration_id=registration.id,
            reviewer_id=reviewer_id,
            request_id=request_id,
        ) from exc
    if outcome.used_exception:
        await append_audit(
            session,
            action="approval.self_exception.used",
            resource_type="tenant_registration_request",
            resource_id=str(registration.id),
            actor_realm="platform",
            actor_id=reviewer_id,
            result="success",
            metadata={"reason": truncate_text((reason or "").strip(), MAX_REASON)},
            request_id=request_id,
        )


async def _deny_self_approval(denied: _SelfApprovalDenied) -> NoReturn:
    """拒绝审计落在**已回滚的事务之外**：这一笔什么都没改，但"谁试过自批"必须留痕。

    审计写失败**不改变拒绝结果**（与 `data_scope.denied`、`auth.permission.denied` 同一口径）：
    拒绝已经生效，把基础设施故障透给审核人只会得到 500 + 一次重试，而简报承诺的是干净的
    403/422；故障本身进日志（只记异常类型，不带 traceback 里的 SQL 参数）。
    """

    try:
        async with db_session(platform=True) as session:
            await append_audit(
                session,
                action="approval.self_approval.denied",
                resource_type="tenant_registration_request",
                resource_id=str(denied.registration_id),
                actor_realm="platform",
                actor_id=denied.reviewer_id,
                result="denied",
                metadata={"code": denied.code},
                request_id=denied.request_id,
            )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "自批拒绝审计写入失败：%s.%s",
            type(exc).__module__,
            type(exc).__name__,
            extra={"registration_id": str(denied.registration_id)},
        )
    raise OnboardingError(denied.code, denied.message, denied.http_status) from denied


async def _call_review(
    session: AsyncSession,
    *,
    request_id_uuid: UUID,
    result: ReviewResult,
    reason: str | None,
    reviewer_id: UUID,
    http_request_id: str | None,
    review_record_id: UUID,
    snapshot_key: str,
    admin_id: UUID | None,
    password_hash: str | None,
    school_code: str | None,
) -> dict[str, Any]:
    """调用数据库审核函数并映射业务错误。"""

    payload = await session.scalar(
        text(
            "select review_registration_request("
            ":request_id, :result, :reason, :reviewer_id, :http_request_id, "
            ":review_record_id, :snapshot_key, :admin_id, :password_hash, :school_code)"
        ),
        {
            "request_id": request_id_uuid,
            "result": result,
            "reason": reason,
            "reviewer_id": reviewer_id,
            "http_request_id": http_request_id,
            "review_record_id": review_record_id,
            "snapshot_key": snapshot_key,
            "admin_id": admin_id,
            "password_hash": password_hash,
            "school_code": school_code,
        },
    )
    if not isinstance(payload, dict):
        raise OnboardingError("REVIEW_FAILED", "审核失败，请重试", 500)
    error = payload.get("error")
    if error is not None:
        status, message = _REVIEW_ERRORS.get(str(error), (500, "审核失败"))
        raise OnboardingError(str(error), message, status)
    return payload


async def _snapshot_key(
    store: ObjectStore, registration: TenantRegistrationRequest, review_record_id: UUID
) -> str:
    """复制凭证快照并返回对象 key（审核依据不随原文件变化）。"""

    suffix = Path(registration.qualification_object_key).suffix
    key = f"reviews/{review_record_id}/qualification{suffix}"
    await store.copy(registration.qualification_object_key, key)
    return key


async def _write_email_failure_audit(
    *,
    reviewer_id: UUID,
    tenant_id: UUID | None,
    request_id_uuid: UUID,
    contact_email: str,
    request_id: str | None,
) -> None:
    """邮件失败回滚后补写失败审计（不包含密码）。"""

    async with db_session(platform=True) as session:
        await append_audit(
            session,
            action="tenant.approve.email_failed",
            resource_type="tenant_registration_request",
            resource_id=str(request_id_uuid),
            actor_realm="platform",
            actor_id=reviewer_id,
            tenant_id=tenant_id,
            result="failure",
            metadata={
                "registration_request_id": str(request_id_uuid),
                "contact_email_masked": mask_email(contact_email),
            },
            request_id=request_id,
        )


async def approve_request(
    request_id_uuid: UUID,
    *,
    reviewer_id: UUID,
    reason: str | None = None,
    request_id: str | None = None,
    store: ObjectStore | None = None,
) -> ApprovalOutcome:
    """审核通过并开通租户（幂等：同一申请只能通过一次）。

    邮件在事务内发送：发送失败 → 回滚开通 + 单独写失败审计 + 502，申请保持 pending。

    参数:
        request_id_uuid: 注册申请 id。
        reviewer_id: 审核人（平台用户 id）。
        reason: 可选备注（写入审核记录）。
        request_id: HTTP request_id（审计关联）。
        store: 可选对象存储实现（测试注入）。

    返回值:
        ApprovalOutcome（含学校编号、登录名、邮件是否发送成功）。

    异常:
        OnboardingError: 申请不存在（404）、已被审核（409）、邮件发送失败（502）。
    """

    settings = get_settings()
    object_store = store or build_object_store(settings)
    initial_password = generate_initial_password()
    # bcrypt cost=12 约数百毫秒，放到线程池执行，避免阻塞事件循环（低频管理操作）
    password_hash = await anyio.to_thread.run_sync(hash_password, initial_password)
    admin_id = uuid4()
    review_record_id = uuid4()
    mailer = build_mailer(settings)

    school_code = ""
    contact_email = ""
    school_name = ""
    tenant_id: UUID | None = None
    try:
        async with db_session(platform=True) as session:
            registration = await session.get(TenantRegistrationRequest, request_id_uuid)
            if registration is None:
                raise OnboardingError("REQUEST_NOT_FOUND", "注册申请不存在", 404)
            if registration.status != "pending":
                raise OnboardingError("ALREADY_REVIEWED", "该申请已被审核", 409)
            await _guard_self_approval(
                session,
                registration=registration,
                reviewer_id=reviewer_id,
                reason=reason,
                request_id=request_id,
            )

            contact_email = registration.email
            school_name = registration.school_name
            snapshot_key = await _snapshot_key(object_store, registration, review_record_id)

            for _ in range(3):
                school_code = await _unique_school_code(session)
                try:
                    async with session.begin_nested():
                        payload = await _call_review(
                            session,
                            request_id_uuid=request_id_uuid,
                            result="approved",
                            reason=truncate_text(reason, MAX_REASON),
                            reviewer_id=reviewer_id,
                            http_request_id=request_id,
                            review_record_id=review_record_id,
                            snapshot_key=snapshot_key,
                            admin_id=admin_id,
                            password_hash=password_hash,
                            school_code=school_code,
                        )
                except IntegrityError:
                    # 学校编号唯一约束冲突：换号重试（邮箱冲突已被申请唯一索引拦截）
                    continue
                tenant_id = UUID(str(payload["tenant_id"]))
                break
            else:
                raise OnboardingError("CODE_EXHAUSTED", "学校编号生成失败，请重试", 503)

            subject, body = render_tenant_approved_email(
                school_name=school_name,
                school_code=school_code,
                login_id=contact_email,
                initial_password=initial_password,
            )
            try:
                await mailer.send(to=contact_email, subject=subject, body=body)
            except Exception as exc:  # 统一映射为可重试业务错误
                raise _EmailDeliveryFailed from exc
    except _SelfApprovalDenied as denied:
        await _deny_self_approval(denied)
    except _EmailDeliveryFailed as exc:
        # 事务已回滚：租户不存在，失败审计不能带 tenant_id（否则违反外键）
        await _write_email_failure_audit(
            reviewer_id=reviewer_id,
            tenant_id=None,
            request_id_uuid=request_id_uuid,
            contact_email=contact_email,
            request_id=request_id,
        )
        raise OnboardingError(
            "EMAIL_DELIVERY_FAILED",
            "初始密码邮件发送失败，开通已回滚，请稍后重试",
            502,
        ) from exc

    return ApprovalOutcome(
        tenant_id=tenant_id,
        school_code=school_code,
        login_id=contact_email,
        email_sent=True,
    )


async def reject_request(
    request_id_uuid: UUID,
    *,
    reviewer_id: UUID,
    reason: str,
    request_id: str | None = None,
    store: ObjectStore | None = None,
) -> None:
    """审核拒绝（理由必填）。

    异常:
        OnboardingError: 理由为空（422）、申请不存在（404）、已被审核（409）、
            提交人自批（403/422，见 RBAC-03）。
    """

    clean_reason = reason.strip()
    if not clean_reason or len(clean_reason) > MAX_REASON or _has_control_chars(clean_reason):
        raise OnboardingError(
            "REASON_REQUIRED", f"拒绝必须填写理由（不超过 {MAX_REASON} 字）", 422
        )

    settings = get_settings()
    object_store = store or build_object_store(settings)
    review_record_id = uuid4()

    try:
        async with db_session(platform=True) as session:
            registration = await session.get(TenantRegistrationRequest, request_id_uuid)
            if registration is None:
                raise OnboardingError("REQUEST_NOT_FOUND", "注册申请不存在", 404)
            if registration.status != "pending":
                raise OnboardingError("ALREADY_REVIEWED", "该申请已被审核", 409)
            await _guard_self_approval(
                session,
                registration=registration,
                reviewer_id=reviewer_id,
                reason=clean_reason,
                request_id=request_id,
            )

            snapshot_key = await _snapshot_key(object_store, registration, review_record_id)
            await _call_review(
                session,
                request_id_uuid=request_id_uuid,
                result="rejected",
                reason=clean_reason,
                reviewer_id=reviewer_id,
                http_request_id=request_id,
                review_record_id=review_record_id,
                snapshot_key=snapshot_key,
                admin_id=None,
                password_hash=None,
                school_code=None,
            )
    except _SelfApprovalDenied as denied:
        await _deny_self_approval(denied)


async def list_registration_requests(
    *, status: str | None = None, limit: int = 100
) -> list[TenantRegistrationRequest]:
    """平台侧：列出注册申请（可按状态过滤，新提交在前）。"""

    async with db_session(platform=True) as session:
        statement = select(TenantRegistrationRequest).order_by(
            TenantRegistrationRequest.submitted_at.desc()
        )
        if status:
            statement = statement.where(TenantRegistrationRequest.status == status)
        rows = (await session.execute(statement.limit(limit))).scalars().all()
        return list(rows)


async def list_tenants(*, status: str | None = None, limit: int = 100) -> list[Tenant]:
    """平台侧：列出租户。"""

    async with db_session(platform=True) as session:
        statement = select(Tenant).order_by(Tenant.created_at.desc())
        if status:
            statement = statement.where(Tenant.status == status)
        rows = (await session.execute(statement.limit(limit))).scalars().all()
        return list(rows)


async def list_review_records(tenant_id: UUID) -> list[SchoolReviewRecord]:
    """平台侧：某租户的审核记录。"""

    async with db_session(platform=True) as session:
        rows = (
            await session.execute(
                select(SchoolReviewRecord)
                .where(SchoolReviewRecord.tenant_id == tenant_id)
                .order_by(SchoolReviewRecord.reviewed_at.desc())
            )
        ).scalars().all()
        return list(rows)
