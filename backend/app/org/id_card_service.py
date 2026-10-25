"""身份证写入/查看服务层（M2 ORG-04）。

所有函数从 `get_settings().id_card_kek` 读密钥；
密钥缺失时 raise `IdCardNotConfigured`（路由层映射 501，不阻塞其他功能）。

AAD 约定（复审 B1）：密文一律绑定 `idcard:{tenant_id}:{档案行 id}`——教师=Teacher.id、
学生=Student.id；建档路径预生成档案 id 后再加密，保证写入与受控查看用同一身份。
"""

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db.models.org import Student, Teacher
from app.db.repository import TenantScopedRepository
from app.db.session import db_session
from app.org.service import OrgActor, OrgError, _audit, _require_tenant
from app.security.id_card import (
    encrypt_id_card,
    hmac_subkey,
    id_card_hmac,
    mask_id_card,
    validate_id_card,
)

logger = logging.getLogger(__name__)


class IdCardNotConfigured(RuntimeError):
    """ID_CARD_KEK 未配置（路由层映射 501）。"""


class IdCardDuplicateError(OrgError):
    """同租内同号已存在（HMAC 查重命中 / 唯一索引兜底）。"""

    def __init__(self) -> None:
        super().__init__("ID_CARD_DUPLICATE", "该身份证号已录入", 409, field="id_card_number")


@dataclass(frozen=True, slots=True)
class IdCardView:
    """完整查看结果（明文，仅受控端点返回）。"""

    personnel_id: UUID
    number: str
    mask: str


def validate_or_422(number: str) -> str:
    """格式校验并翻译为 OrgError 422（路由/导入统一走 ID_CARD_INVALID 行错误）。"""

    try:
        return validate_id_card(number)
    except ValueError as exc:
        raise OrgError("ID_CARD_INVALID", str(exc), 422, field="id_card_number") from exc


def _kek() -> bytes:
    """读 ID_CARD_KEK 并解码；缺失时 raise IdCardNotConfigured。"""

    from app.security.encryption import decode_kek
    from app.security.id_card import IdCardCryptoError

    kek = get_settings().id_card_kek
    if kek is None:
        raise IdCardNotConfigured("ID_CARD_KEK 未配置，身份证功能不可用")
    try:
        return decode_kek(kek)
    except IdCardCryptoError as exc:
        raise IdCardNotConfigured(str(exc)) from exc


def _hmac_key(kek: bytes) -> bytes:
    return hmac_subkey(kek)


def _is_idcard_hmac_violation(exc: IntegrityError) -> bool:
    """唯一索引兜底是否命中身份证指纹列（区别于 login_id 等其他唯一冲突）。"""

    orig = getattr(exc, "orig", None)
    name = getattr(orig, "constraint_name", None) or getattr(orig, "index_name", None) or str(orig)
    return "idcard_hmac" in str(name)


async def _check_hmac_unique(
    session: Any,
    tenant_id: UUID,
    hmac_hex: str,
    *,
    exclude_teacher_id: UUID | None = None,
    exclude_student_id: UUID | None = None,
) -> None:
    """同租内 HMAC 查重（教师+学生两表）；命中即 IdCardDuplicateError。

    exclude_*：更新场景排除自身行；建档场景不传（新行尚未入库）。
    """

    teacher_query = select(Teacher.id).where(
        Teacher.tenant_id == tenant_id, Teacher.id_card_hmac == hmac_hex
    )
    if exclude_teacher_id is not None:
        teacher_query = teacher_query.where(Teacher.id != exclude_teacher_id)
    if (await session.execute(teacher_query)).scalars().first() is not None:
        raise IdCardDuplicateError()

    student_query = select(Student.id).where(
        Student.tenant_id == tenant_id, Student.id_card_hmac == hmac_hex
    )
    if exclude_student_id is not None:
        student_query = student_query.where(Student.id != exclude_student_id)
    if (await session.execute(student_query)).scalars().first() is not None:
        raise IdCardDuplicateError()


def _set_id_card(
    target: Teacher | Student,
    *,
    number: str,
    tenant_id: UUID,
    personnel_id: UUID,
) -> str:
    """写入身份证三件套（enc/hmac/mask）；number 须已归一化；返回掩码（供审计）。

    AAD 绑定档案行 id（复审 B1）：调用方保证 personnel_id == target.id。
    """

    kek = _kek()
    hmac_key = _hmac_key(kek)
    token = encrypt_id_card(
        number, kek=kek, tenant_id=str(tenant_id), personnel_id=str(personnel_id)
    )
    hmac_hex = id_card_hmac(number, hmac_key=hmac_key, tenant_id=str(tenant_id))
    mask = mask_id_card(number)
    target.id_card_enc = token
    target.id_card_hmac = hmac_hex
    target.id_card_mask = mask
    return mask


async def set_teacher_id_card(principal: OrgActor, teacher_id: UUID, *, number: str) -> str:
    """设置/更新教师身份证（写入三件套 + HMAC 查重 + 审计掩码）。返回掩码。"""

    tenant_id = _require_tenant(principal)
    normalized = validate_or_422(number)
    kek = _kek()
    hmac_hex = id_card_hmac(normalized, hmac_key=_hmac_key(kek), tenant_id=str(tenant_id))

    try:
        async with db_session(tenant_id=tenant_id) as session:
            teacher_repository: TenantScopedRepository[Teacher] = TenantScopedRepository(
                session,
                Teacher,
                tenant_id,
                scope=principal.scope_context,
            )
            teacher = await teacher_repository.get(teacher_id)
            if teacher is None:
                raise OrgError("TEACHER_NOT_FOUND", "教师不存在", 404)

            # HMAC 查重（排除自身；唯一索引兜底并发窗口，见 migration uq_teachers_idcard_hmac）
            await _check_hmac_unique(session, tenant_id, hmac_hex, exclude_teacher_id=teacher_id)
            is_create = not teacher.id_card_enc
            mask = _set_id_card(
                teacher, number=normalized, tenant_id=tenant_id, personnel_id=teacher_id
            )
            await _audit(
                session,
                principal,
                tenant_id,
                action="org.id_card.created" if is_create else "org.id_card.updated",
                resource_type="org_teacher",
                resource_id=str(teacher_id),
                metadata={"mask": mask, "employee_no": teacher.employee_no},
            )
            return str(mask)
    except IntegrityError as exc:
        if _is_idcard_hmac_violation(exc):
            raise IdCardDuplicateError() from exc
        raise


async def set_student_id_card(principal: OrgActor, student_id: UUID, *, number: str) -> str:
    """设置/更新学生身份证。"""

    tenant_id = _require_tenant(principal)
    normalized = validate_or_422(number)
    kek = _kek()
    hmac_hex = id_card_hmac(normalized, hmac_key=_hmac_key(kek), tenant_id=str(tenant_id))

    try:
        async with db_session(tenant_id=tenant_id) as session:
            student_repository: TenantScopedRepository[Student] = TenantScopedRepository(
                session,
                Student,
                tenant_id,
                scope=principal.scope_context,
            )
            student = await student_repository.get(student_id)
            if student is None:
                raise OrgError("STUDENT_NOT_FOUND", "学生不存在", 404)

            await _check_hmac_unique(session, tenant_id, hmac_hex, exclude_student_id=student_id)
            is_create = not student.id_card_enc
            mask = _set_id_card(
                student, number=normalized, tenant_id=tenant_id, personnel_id=student_id
            )
            await _audit(
                session,
                principal,
                tenant_id,
                action="org.id_card.created" if is_create else "org.id_card.updated",
                resource_type="org_student",
                resource_id=str(student_id),
                metadata={"mask": mask, "student_no": student.student_no},
            )
            return str(mask)
    except IntegrityError as exc:
        if _is_idcard_hmac_violation(exc):
            raise IdCardDuplicateError() from exc
        raise


async def read_teacher_id_card(principal: OrgActor, teacher_id: UUID, *, reason: str) -> IdCardView:
    """受控完整查看（D3）：解密返回明文；审计含 reason/操作者/目标。"""

    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        teacher_repository: TenantScopedRepository[Teacher] = TenantScopedRepository(
            session,
            Teacher,
            tenant_id,
            scope=principal.scope_context,
        )
        teacher = await teacher_repository.get(teacher_id)
        if teacher is None or not teacher.id_card_enc:
            raise OrgError("ID_CARD_NOT_FOUND", "该人员未录入身份证", 404)

        number = _decrypt(str(teacher.id_card_enc), tenant_id=tenant_id, personnel_id=teacher_id)
        await _audit(
            session,
            principal,
            tenant_id,
            action="sensitive.id_card.read",
            resource_type="org_teacher",
            resource_id=str(teacher_id),
            metadata={
                "reason": reason,
                "mask": teacher.id_card_mask,
                "employee_no": teacher.employee_no,
            },
        )
        return IdCardView(
            personnel_id=teacher_id,
            number=number,
            mask=str(teacher.id_card_mask) if teacher.id_card_mask else "",
        )


async def read_student_id_card(principal: OrgActor, student_id: UUID, *, reason: str) -> IdCardView:
    """学生身份证受控查看。"""

    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        student_repository: TenantScopedRepository[Student] = TenantScopedRepository(
            session,
            Student,
            tenant_id,
            scope=principal.scope_context,
        )
        student = await student_repository.get(student_id)
        if student is None or not student.id_card_enc:
            raise OrgError("ID_CARD_NOT_FOUND", "该人员未录入身份证", 404)

        number = _decrypt(str(student.id_card_enc), tenant_id=tenant_id, personnel_id=student_id)
        await _audit(
            session,
            principal,
            tenant_id,
            action="sensitive.id_card.read",
            resource_type="org_student",
            resource_id=str(student_id),
            metadata={
                "reason": reason,
                "mask": student.id_card_mask,
                "student_no": student.student_no,
            },
        )
        return IdCardView(
            personnel_id=student_id,
            number=number,
            mask=str(student.id_card_mask) if student.id_card_mask else "",
        )


def _decrypt(token: str, *, tenant_id: UUID, personnel_id: UUID) -> str:
    from app.security.id_card import decrypt_id_card

    return decrypt_id_card(
        token, kek=_kek(), tenant_id=str(tenant_id), personnel_id=str(personnel_id)
    )
