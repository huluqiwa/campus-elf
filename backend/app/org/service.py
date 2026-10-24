"""组织结构与人员档案的服务层（M2 ORG-01/03）。

口径（`docs/design-briefs/M2-ORG01-03.md`，已确认）：

- **D1**：班级/任教关系与 class 范围行的增删在**同一事务**完成（`scopes_sync.sync_class_scope_rows`）；
- **D2**：班主任/辅导员权威在 `classes`；学生响应不回冗余列；
- **D3**：`id_card_*` 预留列位**零读写**（ORG-04 交付）；
- **D4**：教师/学生档案与账号 1:1 同建——创建即建 `users` 行（首登强制改密）并绑定内置角色
  （角色行的 self 范围出厂默认生效）；停用账号走权限账号功能点，本模块不做删除；
- 列表分页一律 cursor（TD-070）；班级/课程/教师目录类列表仅全校范围可见，
  人员可见性过滤走 `/school/personnel` 与 `/school/students`（class/self/school）；
- `/school/students` 是 B 端**管理**列表（持有 `school.personnel.view`），回显学号/登录名；
  `/school/personnel` 的「不含 login_id」口径（M1-E2 ⑥）只钉那个端点，不因本模块改变。
"""

import logging
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

import anyio
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import append_audit
from app.auth.passwords import generate_initial_password, hash_password
from app.db.models.identity import User
from app.db.models.org import Course, OrgClass, Student, Teacher, TeacherClassCourse
from app.db.models.rbac import Role, UserRole
from app.db.repository import ScopeBypass, ScopeDecision, TenantScopedRepository
from app.db.session import db_session
from app.org.scopes_sync import build_user_class_member_factory, sync_class_scope_rows
from app.rbac.scopes import DataScopeContext

# 列表分页上界（与 personnel 同口径）
MAX_PAGE_SIZE = 100

# 「字段未提供」哨兵：PUT 语义下区分「不改」与「清空为 NULL」
UNSET: Any = object()

# 审计里放业务字段（姓名/工号/班级名），绝不含口令/令牌/身份证（D3 列零读写）。
logger = logging.getLogger(__name__)


class OrgError(RuntimeError):
    """组织结构的业务错误（映射为 HTTP 状态码）。

    `field` 可选：标注出错字段（导入错误行报告的结构化字段来源；None=行级错误）。
    """

    def __init__(
        self, code: str, message: str, http_status: int, *, field: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.field = field


@dataclass(frozen=True, slots=True)
class ClassItem:
    id: UUID
    grade: str
    name: str
    head_teacher_id: UUID | None
    counselor_id: UUID | None


@dataclass(frozen=True, slots=True)
class CourseItem:
    id: UUID
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class TeacherItem:
    id: UUID
    user_id: UUID
    login_id: str
    employee_no: str
    name: str
    age: int | None
    gender: str | None
    position: str | None
    department_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class StudentItem:
    id: UUID
    user_id: UUID
    login_id: str
    student_no: str
    name: str
    gender: str | None
    class_id: UUID | None


@dataclass(frozen=True, slots=True)
class AssignmentItem:
    id: UUID
    teacher_id: UUID
    class_id: UUID
    course_id: UUID
    term: str


@dataclass(frozen=True, slots=True)
class Page:
    """cursor 分页信封（TD-070）。"""

    items: list[Any]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class CreatedProfile:
    """建档结果：档案项 + 只出现一次的初始密码（D4）。"""

    item: TeacherItem | StudentItem
    initial_password: str


class OrgActor(Protocol):
    """组织写路径的执行者最小结构（请求主体或导入任务的系统执行者）。

    结构化协议（只读成员）：`Principal`（每请求解析，property 成员）与
    `ImportActor`（Arq 任务内代表上传者执行）都天然满足；服务层不关心令牌细节。
    """

    @property
    def user_id(self) -> UUID: ...

    @property
    def realm(self) -> str: ...

    @property
    def tenant_id(self) -> UUID | None: ...

    @property
    def scope_context(self) -> ScopeDecision: ...


def _require_tenant(principal: OrgActor) -> UUID:
    if principal.realm != "tenant" or principal.tenant_id is None:
        raise OrgError("REALM_MISMATCH", "组织管理只在校方租户上下文内可用", 403)
    return principal.tenant_id


def _require_school_scope(principal: OrgActor) -> UUID:
    """目录类读路径（班级/课程/教师/任教列表）仅全校范围：校方管理功能而非个人数据。"""

    tenant_id = _require_tenant(principal)
    scope = principal.scope_context
    if not isinstance(scope, DataScopeContext) or not scope.sees_whole_school:
        raise OrgError("SCOPE_MISMATCH", "该目录仅全校范围可见", 403)
    return tenant_id


def _repository(
    session: AsyncSession,
    model: type[Any],
    tenant_id: UUID,
    scope: ScopeDecision,
) -> TenantScopedRepository[Any]:
    return TenantScopedRepository(session, model, tenant_id, scope=scope)


def _map_integrity_error(exc: Exception, code: str, message: str) -> OrgError:
    """唯一约束冲突 → 409（约束名与原异常不外泄，只记异常类型）。"""

    logger.warning("组织写入唯一约束冲突：%s.%s", type(exc).__module__, type(exc).__name__)
    return OrgError(code, message, 409)


async def _audit(
    session: AsyncSession,
    principal: OrgActor,
    tenant_id: UUID,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    await append_audit(
        session,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        actor_realm="tenant",
        actor_id=principal.user_id,
        tenant_id=tenant_id,
        result="success",
        metadata=metadata,
        request_id=None,
    )


# 建档/建班路径的存在性核验探针：可能需要看到调用方范围之外的行（如目标班级是否在本校），
# 显式绕过调用者范围——只读主键与状态位，不读档案内容。
_PROBE_REASON = "建档/指派前的存在性核验：只读主键与状态位，不读档案内容"


async def _exists(
    session: AsyncSession, model: type[Any], object_id: UUID, tenant_id: UUID
) -> bool:
    """按主键的存在性探针（走仓储；租户边界由仓储强制）。"""

    repository = _repository(session, model, tenant_id, ScopeBypass(reason=_PROBE_REASON))
    return await repository.get(object_id) is not None


# ---------------------------------------------------------------------------
# 班级（班主任/辅导员权威源，D2）
# ---------------------------------------------------------------------------

async def _resolve_teacher_user_ids(
    session: AsyncSession, tenant_id: UUID, teacher_ids: list[UUID]
) -> list[UUID]:
    """把「教师档案 id」解析为「用户账号 id」。

    API 语义：班主任/辅导员按**教师档案**指派（前端选的是老师，不是账号）；
    `classes.head_teacher_id` 落的是 users.id（账号），由这里解析。
    校验档案对应账号为本校在职教师（user_type/status）。

    异常:
        OrgError: 档案不存在 / 账号不是教师 / 账号未启用（422）。
    """

    teacher_repository: TenantScopedRepository[Teacher] = _repository(
        session, Teacher, tenant_id, ScopeBypass(reason=_PROBE_REASON)
    )
    user_repository: TenantScopedRepository[User] = _repository(
        session, User, tenant_id, ScopeBypass(reason=_PROBE_REASON)
    )
    resolved: list[UUID] = []
    for teacher_id in teacher_ids:
        teacher = await teacher_repository.get(teacher_id)
        if teacher is None:
            raise OrgError("TEACHER_NOT_FOUND", "指派的教师不存在", 422)
        user = await user_repository.get(teacher.user_id)
        if user is None:  # pragma: no cover - 复合外键保证档案必有账号
            raise OrgError("TEACHER_NOT_FOUND", "指派的教师不存在", 422)
        if user.user_type != "teacher":
            raise OrgError("TEACHER_TYPE_INVALID", "班主任/辅导员必须是教师账号", 422)
        if user.status != "active":
            raise OrgError("TEACHER_INACTIVE", "指派的教师账号未启用", 422)
        resolved.append(teacher.user_id)
    return resolved


async def _teacher_profile_ids_for_users(
    session: AsyncSession, tenant_id: UUID, user_ids: list[UUID]
) -> dict[UUID, UUID]:
    """users.id → 教师档案 id 的反向映射（对外身份统一为教师档案 id）。"""

    if not user_ids:
        return {}
    repository: TenantScopedRepository[Teacher] = _repository(
        session, Teacher, tenant_id,
        ScopeBypass(reason="班级响应回填教师档案 id（存储列是账号 id），仅限已可见行"),
    )
    teachers = (
        await session.execute(
            repository.base_query().where(Teacher.user_id.in_(user_ids))
        )
    ).scalars().all()
    return {teacher.user_id: teacher.id for teacher in teachers}


async def _class_item(session: AsyncSession, tenant_id: UUID, org_class: OrgClass) -> ClassItem:
    """对外视图：head_teacher_id/counselor_id 为**教师档案 id**（存储列是 users.id）。"""

    mapping = await _teacher_profile_ids_for_users(
        session, tenant_id,
        [u for u in (org_class.head_teacher_id, org_class.counselor_id) if u is not None],
    )
    return ClassItem(
        id=org_class.id, grade=org_class.grade, name=org_class.name,
        head_teacher_id=mapping.get(org_class.head_teacher_id)
        if org_class.head_teacher_id is not None else None,
        counselor_id=mapping.get(org_class.counselor_id)
        if org_class.counselor_id is not None else None,
    )


async def create_class(
    principal: OrgActor,
    *,
    grade: str,
    name: str,
    head_teacher_id: UUID | None,
    counselor_id: UUID | None,
) -> ClassItem:
    tenant_id = _require_tenant(principal)
    if head_teacher_id is not None and head_teacher_id == counselor_id:
        raise OrgError("CLASS_SAME_TEACHER", "班主任与辅导员不能是同一位教师", 422)

    async with db_session(tenant_id=tenant_id) as session:
        resolved_ids = await _resolve_teacher_user_ids(
            session, tenant_id, [u for u in (head_teacher_id, counselor_id) if u is not None]
        )
        head_user_id = resolved_ids[0] if head_teacher_id is not None else None
        counselor_user_id = (
            resolved_ids[-1] if counselor_id is not None and head_teacher_id is not None else (
                resolved_ids[0] if counselor_id is not None else None
            )
        )
        repository: TenantScopedRepository[OrgClass] = _repository(
            session, OrgClass, tenant_id, principal.scope_context
        )
        org_class = OrgClass(
            tenant_id=tenant_id,
            grade=grade,
            name=name,
            head_teacher_id=head_user_id,
            counselor_id=counselor_user_id,
        )
        try:
            await repository.add(org_class)
        except Exception as exc:
            raise _map_integrity_error(exc, "ORG_CONFLICT", "同年级下班级名已存在") from exc
        await sync_class_scope_rows(session, tenant_id)
        await _audit(
            session, principal, tenant_id,
            action="org.class.created", resource_type="org_class",
            resource_id=str(org_class.id),
            metadata={"grade": grade, "name": name,
                      "head_teacher_id": str(head_teacher_id) if head_teacher_id else None,
                      "counselor_id": str(counselor_id) if counselor_id else None},
        )
        return await _class_item(session, tenant_id, org_class)


async def update_class(
    principal: OrgActor,
    class_id: UUID,
    *,
    grade: Any = UNSET,
    name: Any = UNSET,
    head_teacher_id: Any = UNSET,
    counselor_id: Any = UNSET,
) -> ClassItem:
    """更新班级；显式传 None 表示清空班主任/辅导员，缺省（UNSET）表示不改。"""

    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        repository: TenantScopedRepository[OrgClass] = _repository(
            session, OrgClass, tenant_id, principal.scope_context
        )
        org_class = await repository.get(class_id)
        if org_class is None:
            raise OrgError("ORG_NOT_FOUND", "班级不存在", 404)

        before_grade: str = org_class.grade
        before_name: str = org_class.name
        before_head: UUID | None = org_class.head_teacher_id
        before_counselor: UUID | None = org_class.counselor_id
        changed: list[str] = []
        if grade is not UNSET and grade != org_class.grade:
            org_class.grade = grade
            changed.append("grade")
        if name is not UNSET and name != org_class.name:
            org_class.name = name
            changed.append("name")

        # 班主任/辅导员：API 收教师档案 id，先解析成 user_id 再落列（防 autoflush 期 FK 失败）。
        # 相等比较必须在解析**之后**：跨 id 空间（档案 id vs users.id）恒不相等，会制造空审计。
        desired: dict[str, UUID | None] = {}
        pending: list[UUID] = []
        for field_name in ("head_teacher_id", "counselor_id"):
            value: Any = {"head_teacher_id": head_teacher_id, "counselor_id": counselor_id}[
                field_name
            ]
            if value is UNSET:
                continue
            desired[field_name] = value
            if value is not None:
                pending.append(value)
        if pending:
            resolved = await _resolve_teacher_user_ids(session, tenant_id, pending)
            by_teacher = dict(zip(pending, resolved, strict=True))
            for field_name, teacher_profile_id in desired.items():
                new_user_id = (
                    None if teacher_profile_id is None else by_teacher[teacher_profile_id]
                )
                if new_user_id != getattr(org_class, field_name):
                    setattr(org_class, field_name, new_user_id)
                    changed.append(field_name)
            if (
                org_class.head_teacher_id is not None
                and org_class.head_teacher_id == org_class.counselor_id
            ):
                raise OrgError("CLASS_SAME_TEACHER", "班主任与辅导员不能是同一位教师", 422)

        if not changed:
            return await _class_item(session, tenant_id, org_class)

        sync = await sync_class_scope_rows(session, tenant_id)
        # before/after 的班主任/辅导员统一用**教师档案 id**（与 created 审计及 API 同口径）
        after_item = await _class_item(session, tenant_id, org_class)
        before_mapping = await _teacher_profile_ids_for_users(
            session, tenant_id,
            [u for u in (before_head, before_counselor) if u is not None],
        )
        await _audit(
            session, principal, tenant_id,
            action="org.class.updated", resource_type="org_class",
            resource_id=str(class_id),
            metadata={
                "changed": changed,
                "before": {
                    "grade": before_grade, "name": before_name,
                    "head_teacher_id": str(before_mapping[before_head])
                    if before_head is not None else None,
                    "counselor_id": str(before_mapping[before_counselor])
                    if before_counselor is not None else None,
                },
                "after": {"grade": after_item.grade, "name": after_item.name,
                          "head_teacher_id": str(after_item.head_teacher_id)
                          if after_item.head_teacher_id else None,
                          "counselor_id": str(after_item.counselor_id)
                          if after_item.counselor_id else None},
                "scope_added": sync.added, "scope_removed": sync.removed,
            },
        )
        return await _class_item(session, tenant_id, org_class)


async def delete_class(principal: OrgActor, class_id: UUID) -> None:
    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        repository: TenantScopedRepository[OrgClass] = _repository(
            session, OrgClass, tenant_id, principal.scope_context
        )
        org_class = await repository.get(class_id)
        if org_class is None:
            raise OrgError("ORG_NOT_FOUND", "班级不存在", 404)
        student_repository: TenantScopedRepository[Student] = _repository(
            session, Student, tenant_id,
            ScopeBypass(reason="删除班级前核验是否仍有在读学生（跨范围安全检查）"),
        )
        remaining = (
            student_repository.base_query()
            .where(Student.class_id == class_id)
            .with_only_columns(func.count())
        )
        student_count: int = (await session.execute(remaining)).scalar_one()
        if student_count:
            raise OrgError("ORG_CLASS_HAS_STUDENTS", "班级仍有学生，先转班或移出后再删除", 409)
        await repository.delete(org_class)
        await sync_class_scope_rows(session, tenant_id)
        await _audit(
            session, principal, tenant_id,
            action="org.class.deleted", resource_type="org_class", resource_id=str(class_id),
            metadata={"grade": org_class.grade, "name": org_class.name},
        )


async def list_classes(
    principal: OrgActor, *, limit: int = 50, cursor: str | None = None
) -> Page:
    tenant_id = _require_school_scope(principal)
    async with db_session(tenant_id=tenant_id) as session:
        repository: TenantScopedRepository[OrgClass] = _repository(
            session, OrgClass, tenant_id, principal.scope_context
        )
        rows, next_cursor = await repository.list_cursor(limit=limit, cursor=cursor)
        return Page(
            items=[await _class_item(session, tenant_id, row) for row in rows],
            next_cursor=next_cursor,
        )


# ---------------------------------------------------------------------------
# 课程（目录数据，非个人数据）
# ---------------------------------------------------------------------------

async def create_course(principal: OrgActor, *, code: str, name: str) -> CourseItem:
    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        repository: TenantScopedRepository[Course] = _repository(
            session, Course, tenant_id, principal.scope_context
        )
        course = Course(tenant_id=tenant_id, code=code, name=name)
        try:
            await repository.add(course)
        except Exception as exc:
            raise _map_integrity_error(exc, "ORG_CONFLICT", "课程编码已存在") from exc
        await _audit(
            session, principal, tenant_id,
            action="org.course.created", resource_type="org_course",
            resource_id=str(course.id), metadata={"code": code, "name": name},
        )
        return CourseItem(id=course.id, code=code, name=name)


async def list_courses(principal: OrgActor, *, limit: int = 50, cursor: str | None = None) -> Page:
    tenant_id = _require_school_scope(principal)
    async with db_session(tenant_id=tenant_id) as session:
        repository: TenantScopedRepository[Course] = _repository(
            session, Course, tenant_id, principal.scope_context
        )
        rows, next_cursor = await repository.list_cursor(limit=limit, cursor=cursor)
        return Page(
            items=[CourseItem(id=row.id, code=row.code, name=row.name) for row in rows],
            next_cursor=next_cursor,
        )


# ---------------------------------------------------------------------------
# 教师 / 学生档案（D4：与账号 1:1 同建）
# ---------------------------------------------------------------------------

async def _bind_builtin_role(
    session: AsyncSession, tenant_id: UUID, user_id: UUID, *, role_code: str
) -> None:
    """新档案绑定内置角色（self 范围随角色出厂默认行生效）。角色缺失=配置错误，fail-loud。"""

    role_repository: TenantScopedRepository[Role] = _repository(
        session, Role, tenant_id, ScopeBypass(reason="建档绑定内置角色：角色目录非个人数据")
    )
    role = (
        await session.execute(
            role_repository.base_query().where(Role.code == role_code, Role.is_system.is_(True))
        )
    ).scalars().first()
    if role is None:
        raise RuntimeError(f"内置角色 {role_code!r} 缺失（开通流程未播种），拒绝建档")
    session.add(UserRole(tenant_id=tenant_id, user_id=user_id, role_id=role.id))


async def _first_by(
    session: AsyncSession, model: type[Any], tenant_id: UUID, predicate: Any
) -> Any | None:
    """按谓词取首行（仓储直查；谓词由调用方用模型属性构造）。"""

    repository: TenantScopedRepository[Any] = _repository(
        session, model, tenant_id,
        ScopeBypass(reason="唯一性占用探针：只读登录名/编号，不读档案内容"),
    )
    return (await session.execute(repository.base_query().where(predicate))).scalars().first()


async def _assert_login_free(session: AsyncSession, tenant_id: UUID, login_id: str) -> None:
    if await _first_by(session, User, tenant_id, User.login_id == login_id) is not None:
        raise OrgError("ORG_LOGIN_TAKEN", "登录名（学号/工号）已被占用", 409, field="login_id")


async def _assert_employee_no_free(session: AsyncSession, tenant_id: UUID, employee_no: str) -> None:
    if await _first_by(session, Teacher, tenant_id, Teacher.employee_no == employee_no) is not None:
        raise OrgError("ORG_NO_TAKEN", "工号已被占用", 409, field="employee_no")


async def _assert_student_no_free(session: AsyncSession, tenant_id: UUID, student_no: str) -> None:
    if await _first_by(session, Student, tenant_id, Student.student_no == student_no) is not None:
        raise OrgError("ORG_NO_TAKEN", "学号已被占用", 409, field="student_no")


async def create_teacher(
    principal: OrgActor,
    *,
    login_id: str,
    employee_no: str,
    name: str,
    age: int | None,
    gender: str | None,
    position: str | None,
    email: str | None,
    department_id: UUID | None = None,
) -> CreatedProfile:
    tenant_id = _require_tenant(principal)
    initial_password = generate_initial_password()
    password_hash = await anyio.to_thread.run_sync(hash_password, initial_password)

    async with db_session(tenant_id=tenant_id) as session:
        await _assert_login_free(session, tenant_id, login_id)
        await _assert_employee_no_free(session, tenant_id, employee_no)

        repository = _repository(session, User, tenant_id, principal.scope_context)
        user = User(
            tenant_id=tenant_id,
            user_type="teacher",
            login_id=login_id,
            display_name=name,
            email=email,
            password_hash=password_hash,
            status="active",
            must_change_password=True,
        )
        await repository.add(user)
        if department_id is not None:
            # 存在性 + 在用状态（M1 复审：停用部门不得获得新归属）
            from app.db.models.org import Department

            dept_repository: TenantScopedRepository[Department] = TenantScopedRepository(
                session, Department, tenant_id, scope=ScopeBypass(reason=_PROBE_REASON),
            )
            dept = await dept_repository.get(department_id)
            if dept is None:
                raise OrgError("DEPARTMENT_NOT_FOUND", "目标部门不存在", 422)
            if dept.status != "active":
                raise OrgError("DEPARTMENT_DISABLED", "目标部门已停用", 422)
        teacher = Teacher(
            tenant_id=tenant_id, user_id=user.id,
            employee_no=employee_no, name=name, age=age, gender=gender, position=position,
            department_id=department_id,
        )
        await repository.add(teacher)
        await _bind_builtin_role(session, tenant_id, user.id, role_code="teacher")
        await _audit(
            session, principal, tenant_id,
            action="org.teacher.created", resource_type="org_teacher",
            resource_id=str(teacher.id),
            metadata={"employee_no": employee_no, "name": name, "login_id": login_id},
        )
        item = TeacherItem(
            id=teacher.id, user_id=user.id, login_id=login_id, employee_no=employee_no,
            name=name, age=age, gender=gender, position=position,
            department_id=teacher.department_id,
        )
    return CreatedProfile(item=item, initial_password=initial_password)


async def create_student(
    principal: OrgActor,
    *,
    login_id: str,
    student_no: str,
    name: str,
    gender: str | None,
    class_id: UUID | None,
    email: str | None,
) -> CreatedProfile:
    tenant_id = _require_tenant(principal)
    initial_password = generate_initial_password()
    password_hash = await anyio.to_thread.run_sync(hash_password, initial_password)

    async with db_session(tenant_id=tenant_id) as session:
        if class_id is not None and not await _exists(session, OrgClass, class_id, tenant_id):
            raise OrgError("ORG_NOT_FOUND", "目标班级不存在", 422)
        await _assert_login_free(session, tenant_id, login_id)
        await _assert_student_no_free(session, tenant_id, student_no)

        repository = _repository(session, User, tenant_id, principal.scope_context)
        user = User(
            tenant_id=tenant_id,
            user_type="student",
            login_id=login_id,
            display_name=name,
            email=email,
            password_hash=password_hash,
            status="active",
            must_change_password=True,
        )
        await repository.add(user)
        student = Student(
            tenant_id=tenant_id, user_id=user.id,
            student_no=student_no, name=name, gender=gender, class_id=class_id,
        )
        await repository.add(student)
        await _bind_builtin_role(session, tenant_id, user.id, role_code="student")
        await _audit(
            session, principal, tenant_id,
            action="org.student.created", resource_type="org_student",
            resource_id=str(student.id),
            metadata={"student_no": student_no, "name": name, "login_id": login_id,
                      "class_id": str(class_id) if class_id else None},
        )
        item = StudentItem(
            id=student.id, user_id=user.id, login_id=login_id, student_no=student_no,
            name=name, gender=gender, class_id=class_id,
        )
    return CreatedProfile(item=item, initial_password=initial_password)


async def _login_ids_for(
    session: AsyncSession, tenant_id: UUID, user_ids: list[UUID]
) -> dict[UUID, str]:
    """一次回填登录名（学号/工号），避免逐行查询。"""

    if not user_ids:
        return {}
    repository: TenantScopedRepository[User] = _repository(
        session, User, tenant_id, ScopeBypass(reason="档案列表回填登录名（学号/工号），仅限已可见行")
    )
    users = (
        await session.execute(
            repository.base_query().where(User.id.in_(user_ids))
        )
    ).scalars().all()
    return {user.id: user.login_id for user in users}


async def list_students(
    principal: OrgActor,
    *,
    limit: int = 50,
    cursor: str | None = None,
) -> Page:
    """学生列表：school 全量 / class 班内学生 / self 本人（fail-closed 空集）。"""

    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        repository: TenantScopedRepository[Student] = TenantScopedRepository(
            session, Student, tenant_id, scope=principal.scope_context,
            owner_attr="user_id",
            class_scope_factory=build_user_class_member_factory(tenant_id),
        )
        rows, next_cursor = await repository.list_cursor(limit=limit, cursor=cursor)
        logins = await _login_ids_for(session, tenant_id, [row.user_id for row in rows])
        return Page(
            items=[
                StudentItem(
                    id=row.id, user_id=row.user_id, login_id=logins.get(row.user_id, ""),
                    student_no=row.student_no, name=row.name, gender=row.gender,
                    class_id=row.class_id,
                )
                for row in rows
            ],
            next_cursor=next_cursor,
        )


async def list_teachers(
    principal: OrgActor, *, limit: int = 50, cursor: str | None = None
) -> Page:
    """教师目录（仅全校范围；条目不含初始密码与身份证列）。"""

    tenant_id = _require_school_scope(principal)
    async with db_session(tenant_id=tenant_id) as session:
        repository: TenantScopedRepository[Teacher] = _repository(
            session, Teacher, tenant_id, principal.scope_context
        )
        rows, next_cursor = await repository.list_cursor(limit=limit, cursor=cursor)
        logins = await _login_ids_for(session, tenant_id, [row.user_id for row in rows])
        return Page(
            items=[
                TeacherItem(
                    id=row.id, user_id=row.user_id, login_id=logins.get(row.user_id, ""),
                    employee_no=row.employee_no, name=row.name, age=row.age,
                    gender=row.gender, position=row.position,
                )
                for row in rows
            ],
            next_cursor=next_cursor,
        )


# ---------------------------------------------------------------------------
# 任教关系（class 范围行的事实来源之一）
# ---------------------------------------------------------------------------

async def create_assignment(
    principal: OrgActor,
    *,
    teacher_id: UUID,
    class_id: UUID,
    course_id: UUID,
    term: str,
) -> AssignmentItem:
    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        for label, model, object_id in (
            ("教师", Teacher, teacher_id),
            ("班级", OrgClass, class_id),
            ("课程", Course, course_id),
        ):
            if not await _exists(session, model, object_id, tenant_id):
                raise OrgError("ORG_NOT_FOUND", f"{label}不存在", 422)
        # 与班主任指派同一口径：停用教师不得被排课（登录侧另有拦截，这里是写路径防线）
        await _resolve_teacher_user_ids(session, tenant_id, [teacher_id])
        repository: TenantScopedRepository[TeacherClassCourse] = _repository(
            session, TeacherClassCourse, tenant_id, principal.scope_context
        )
        assignment = TeacherClassCourse(
            tenant_id=tenant_id, teacher_id=teacher_id,
            class_id=class_id, course_id=course_id, term=term,
        )
        try:
            await repository.add(assignment)
        except Exception as exc:
            raise _map_integrity_error(exc, "ORG_CONFLICT", "该任教关系已存在") from exc
        sync = await sync_class_scope_rows(session, tenant_id)
        await _audit(
            session, principal, tenant_id,
            action="org.assignment.created", resource_type="org_assignment",
            resource_id=str(assignment.id),
            metadata={"teacher_id": str(teacher_id), "class_id": str(class_id),
                      "course_id": str(course_id), "term": term,
                      "scope_added": sync.added, "scope_removed": sync.removed},
        )
        return AssignmentItem(
            id=assignment.id, teacher_id=teacher_id, class_id=class_id,
            course_id=course_id, term=term,
        )


async def delete_assignment(principal: OrgActor, assignment_id: UUID) -> None:
    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        repository: TenantScopedRepository[TeacherClassCourse] = _repository(
            session, TeacherClassCourse, tenant_id, principal.scope_context
        )
        assignment = await repository.get(assignment_id)
        if assignment is None:
            raise OrgError("ORG_NOT_FOUND", "任教关系不存在", 404)
        await repository.delete(assignment)
        sync = await sync_class_scope_rows(session, tenant_id)
        await _audit(
            session, principal, tenant_id,
            action="org.assignment.deleted", resource_type="org_assignment",
            resource_id=str(assignment_id),
            metadata={"teacher_id": str(assignment.teacher_id),
                      "class_id": str(assignment.class_id),
                      "course_id": str(assignment.course_id), "term": assignment.term,
                      "scope_added": sync.added, "scope_removed": sync.removed},
        )


async def list_assignments(
    principal: OrgActor,
    *,
    limit: int = 50,
    cursor: str | None = None,
    teacher_id: UUID | None = None,
) -> Page:
    tenant_id = _require_school_scope(principal)
    async with db_session(tenant_id=tenant_id) as session:
        repository: TenantScopedRepository[TeacherClassCourse] = _repository(
            session, TeacherClassCourse, tenant_id, principal.scope_context
        )
        rows, next_cursor = await repository.list_cursor(
            limit=limit, cursor=cursor,
            extra=TeacherClassCourse.teacher_id == teacher_id if teacher_id is not None else None,
        )
        return Page(
            items=[
                AssignmentItem(
                    id=row.id, teacher_id=row.teacher_id, class_id=row.class_id,
                    course_id=row.course_id, term=row.term,
                )
                for row in rows
            ],
            next_cursor=next_cursor,
        )


__all__ = [
    "MAX_PAGE_SIZE",
    "UNSET",
    "AssignmentItem",
    "ClassItem",
    "CourseItem",
    "CreatedProfile",
    "Page",
    "StudentItem",
    "TeacherItem",
    "create_assignment",
    "create_class",
    "create_course",
    "create_student",
    "create_teacher",
    "delete_assignment",
    "delete_class",
    "list_assignments",
    "list_classes",
    "list_courses",
    "list_students",
    "list_teachers",
    "update_class",
]
