"""校方侧组织结构与人员档案接口（M2 ORG-01/03 + ORG-05 部门树，简报见 docs/design-briefs/）。

权限映射（rbac-matrix §5 行 173–176）：
- 读：`school.personnel.view`；
- 班级/课程/任教关系维护：`school.org.manage`（组织结构语义）；
- 教师/学生建档：`school.personnel.create`。

响应口径：
- `/school/students` 是 B 端**管理**列表，回显学号/登录名；`/school/personnel` 的
  「不含 login_id」口径（M1-E2 ⑥）只钉它自己的端点，本模块不改变；
- 建档响应携带 `initial_password` 且**只出现这一次**（D4；不落日志/审计）；
- 列表一律 cursor 分页（TD-070）。
"""

from dataclasses import asdict
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field, StringConstraints

from app.auth.principal import Principal, require_permission
from app.db.repository import CursorError
from app.org import academic_year, id_card_service, service
from app.org import departments as departments_service
from app.org.departments import DepartmentError
from app.org.id_card_service import IdCardNotConfigured
from app.org.service import OrgError, Page

router = APIRouter(prefix="/school", tags=["school"])

ViewerDep = require_permission("school.personnel.view", realm="tenant")
OrgManageDep = require_permission("school.org.manage", realm="tenant")
PersonnelCreateDep = require_permission("school.personnel.create", realm="tenant")


def _http_error(exc: OrgError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=exc.message)


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------


class ClassResponse(BaseModel):
    id: UUID
    entry_year: int
    grade: str
    name: str
    status: str
    head_teacher_id: UUID | None
    counselor_id: UUID | None


class GradeChangeItem(BaseModel):
    id: UUID
    entry_year: int
    name: str
    from_grade: str
    to_grade: str


class RolloverResponse(BaseModel):
    dry_run: bool
    advanced: list[GradeChangeItem]
    archived: list[GradeChangeItem]
    advanced_count: int
    archived_count: int
    rollover_id: UUID | None = None


class CourseResponse(BaseModel):
    id: UUID
    code: str
    name: str


class TeacherResponse(BaseModel):
    id: UUID
    user_id: UUID
    login_id: str
    employee_no: str
    name: str
    age: int | None
    gender: str | None
    position: str | None
    department_id: UUID | None = None
    initial_password: str | None = None


class StudentResponse(BaseModel):
    id: UUID
    user_id: UUID
    login_id: str
    student_no: str
    name: str
    gender: str | None
    class_id: UUID | None
    initial_password: str | None = None


class AssignmentResponse(BaseModel):
    id: UUID
    teacher_id: UUID
    class_id: UUID
    course_id: UUID
    term: str


class PageResponse[ItemT](BaseModel):
    """cursor 分页信封（TD-070）；按端点参数化条目类型，OpenAPI 契约不退化。"""

    items: list[ItemT]
    next_cursor: str | None


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class ClassCreateRequest(BaseModel):
    entry_year: int = Field(ge=1970, le=2100, description="入学年份（届身份，建档后不可改）")
    grade: str = Field(min_length=1, max_length=16, description="当前年级")
    name: str = Field(min_length=1, max_length=64)
    head_teacher_id: UUID | None = None
    counselor_id: UUID | None = None


class ClassUpdateRequest(BaseModel):
    """PUT 语义：字段缺省=不改；显式传 null=清空班主任/辅导员。"""

    grade: str | None = Field(default=None, min_length=1, max_length=16)
    name: str | None = Field(default=None, min_length=1, max_length=64)
    head_teacher_id: UUID | None = None
    counselor_id: UUID | None = None
    # 仅为拦截：入学年份是届身份不可改（出现即 422，见路由守卫）
    entry_year: int | None = Field(default=None, ge=1970, le=2100)


class RolloverAdvanceRequest(BaseModel):
    """一条升年级映射（可选按入学年份过滤，防误伤新届班）。"""

    from_grade: str = Field(min_length=1, max_length=16)
    to_grade: str = Field(min_length=1, max_length=16)
    entry_year: int | None = Field(default=None, ge=1970, le=2100)


class ClassRolloverRequest(BaseModel):
    """学年流转请求（ORG-06 D4）：dry_run 缺省 True（fail-safe，先预览再执行）。"""

    advances: list[RolloverAdvanceRequest] = Field(default_factory=list)
    archive_grades: list[Annotated[str, StringConstraints(min_length=1, max_length=16)]] = Field(
        default_factory=list
    )
    idempotency_key: str = Field(min_length=8, max_length=64)
    dry_run: bool = True


class CourseCreateRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)


class TeacherCreateRequest(BaseModel):
    login_id: str = Field(min_length=1, max_length=254)
    employee_no: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    age: int | None = Field(default=None, ge=16, le=80)
    gender: str | None = Field(default=None, pattern="^(male|female|other)$")
    position: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=254)
    department_id: UUID | None = None
    # 可选采集（D2-A）：格式/校验码由服务层校验（非法 → 422 ID_CARD_INVALID）
    id_card_number: str | None = Field(default=None, max_length=20)


class StudentCreateRequest(BaseModel):
    login_id: str = Field(min_length=1, max_length=254)
    student_no: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    gender: str | None = Field(default=None, pattern="^(male|female|other)$")
    class_id: UUID | None = None
    email: str | None = Field(default=None, max_length=254)
    id_card_number: str | None = Field(default=None, max_length=20)


class AssignmentCreateRequest(BaseModel):
    teacher_id: UUID
    class_id: UUID
    course_id: UUID
    term: str = Field(min_length=1, max_length=16)


def _class_response(item: Any) -> ClassResponse:
    return ClassResponse(
        id=item.id,
        entry_year=item.entry_year,
        grade=item.grade,
        name=item.name,
        status=item.status,
        head_teacher_id=item.head_teacher_id,
        counselor_id=item.counselor_id,
    )


async def _paged[ItemT](
    coro: Any,
    item_model: type[ItemT],
    *,
    hide_initial_password: bool = False,
) -> PageResponse[ItemT]:
    """列表端点统一包装：服务层调用 + 坏游标（CursorError）→ 400 的错误契约。"""

    try:
        page: Page = await coro
    except OrgError as exc:
        raise _http_error(exc) from exc
    except CursorError as exc:
        raise HTTPException(status_code=400, detail="分页游标无效") from exc
    items: list[dict[str, Any]] = []
    for item in page.items:
        entry = asdict(item)
        if hide_initial_password:
            entry["initial_password"] = None
        items.append(entry)
    return PageResponse[item_model](items=items, next_cursor=page.next_cursor)  # type: ignore[valid-type]


# ---------------------------------------------------------------------------
# 班级
# ---------------------------------------------------------------------------


@router.post("/classes", status_code=201, response_model=ClassResponse)
async def create_class(
    payload: ClassCreateRequest, viewer: Annotated[Principal, OrgManageDep]
) -> ClassResponse:
    try:
        item = await service.create_class(
            viewer,
            entry_year=payload.entry_year,
            grade=payload.grade,
            name=payload.name,
            head_teacher_id=payload.head_teacher_id,
            counselor_id=payload.counselor_id,
        )
    except OrgError as exc:
        raise _http_error(exc) from exc
    return _class_response(item)


@router.get("/classes", response_model=PageResponse[ClassResponse])
async def list_classes(
    viewer: Annotated[Principal, ViewerDep],
    limit: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    entry_year: Annotated[int | None, Query(ge=1970, le=2100)] = None,
    include_archived: Annotated[bool, Query()] = False,
) -> PageResponse[ClassResponse]:
    """班级目录（ORG-06）：默认只回活跃班级，归档届显式包含才可见。"""

    return await _paged(
        service.list_classes(
            viewer,
            limit=limit,
            cursor=cursor,
            entry_year=entry_year,
            include_archived=include_archived,
        ),
        ClassResponse,
    )


@router.put("/classes/{class_id}", response_model=ClassResponse)
async def update_class(
    class_id: UUID,
    payload: ClassUpdateRequest,
    viewer: Annotated[Principal, OrgManageDep],
) -> ClassResponse:
    """字段级 PUT：只更新请求体里出现的字段（缺省=不改；null=清空指派）。"""

    kwargs: dict[str, Any] = {}
    provided = payload.model_fields_set
    if "entry_year" in provided:
        raise HTTPException(status_code=422, detail="入学年份是届身份，建档后不可修改")
    for field_name in ("grade", "name", "head_teacher_id", "counselor_id"):
        if field_name in provided:
            kwargs[field_name] = getattr(payload, field_name)
    try:
        item = await service.update_class(viewer, class_id, **kwargs)
    except OrgError as exc:
        raise _http_error(exc) from exc
    return _class_response(item)


@router.delete("/classes/{class_id}", status_code=204)
async def delete_class(class_id: UUID, viewer: Annotated[Principal, OrgManageDep]) -> None:
    try:
        await service.delete_class(viewer, class_id)
    except OrgError as exc:
        raise _http_error(exc) from exc


@router.post("/classes/rollover", response_model=RolloverResponse)
async def rollover_classes(
    payload: ClassRolloverRequest, viewer: Annotated[Principal, OrgManageDep]
) -> RolloverResponse:
    """学年流转（ORG-06）：先 dry_run 预览，确认后 dry_run=false 执行；幂等键防重复。"""

    advances = tuple(
        academic_year.RolloverAdvance(
            from_grade=a.from_grade, to_grade=a.to_grade, entry_year=a.entry_year
        )
        for a in payload.advances
    )
    try:
        result = await academic_year.execute_rollover(
            viewer,
            advances=advances,
            archive_grades=tuple(payload.archive_grades),
            idempotency_key=payload.idempotency_key,
            dry_run=payload.dry_run,
        )
    except OrgError as exc:
        raise _http_error(exc) from exc
    return RolloverResponse(
        dry_run=result.dry_run,
        advanced=[GradeChangeItem(**asdict(c)) for c in result.advances],
        archived=[GradeChangeItem(**asdict(c)) for c in result.archives],
        advanced_count=len(result.advances),
        archived_count=len(result.archives),
        rollover_id=result.rollover_id,
    )


# ---------------------------------------------------------------------------
# 课程
# ---------------------------------------------------------------------------


@router.post("/courses", status_code=201, response_model=CourseResponse)
async def create_course(
    payload: CourseCreateRequest, viewer: Annotated[Principal, OrgManageDep]
) -> CourseResponse:
    try:
        item = await service.create_course(viewer, code=payload.code, name=payload.name)
    except OrgError as exc:
        raise _http_error(exc) from exc
    return CourseResponse(id=item.id, code=item.code, name=item.name)


@router.get("/courses", response_model=PageResponse[CourseResponse])
async def list_courses(
    viewer: Annotated[Principal, ViewerDep],
    limit: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> PageResponse[CourseResponse]:
    return await _paged(service.list_courses(viewer, limit=limit, cursor=cursor), CourseResponse)


# ---------------------------------------------------------------------------
# 教师
# ---------------------------------------------------------------------------


@router.post("/teachers", status_code=201, response_model=TeacherResponse)
async def create_teacher(
    payload: TeacherCreateRequest, viewer: Annotated[Principal, PersonnelCreateDep]
) -> TeacherResponse:
    try:
        created = await service.create_teacher(
            viewer,
            login_id=payload.login_id,
            employee_no=payload.employee_no,
            name=payload.name,
            age=payload.age,
            gender=payload.gender,
            position=payload.position,
            email=payload.email,
            id_card_number=payload.id_card_number,
        )
    except OrgError as exc:
        raise _http_error(exc) from exc
    except IdCardNotConfigured as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    item = created.item
    if not isinstance(item, service.TeacherItem):  # pragma: no cover - 类型收窄兜底
        raise RuntimeError("教师建档返回了学生档案类型")
    return TeacherResponse(
        id=item.id,
        user_id=item.user_id,
        login_id=item.login_id,
        employee_no=item.employee_no,
        name=item.name,
        age=item.age,
        gender=item.gender,
        position=item.position,
        department_id=getattr(item, "department_id", None),
        initial_password=created.initial_password,
    )


@router.get("/teachers", response_model=PageResponse[TeacherResponse])
async def list_teachers(
    viewer: Annotated[Principal, ViewerDep],
    limit: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> PageResponse[TeacherResponse]:
    """教师目录（仅全校范围；条目不含初始密码）。"""

    return await _paged(
        service.list_teachers(viewer, limit=limit, cursor=cursor),
        TeacherResponse,
        hide_initial_password=True,
    )


# ---------------------------------------------------------------------------
# 学生
# ---------------------------------------------------------------------------


@router.post("/students", status_code=201, response_model=StudentResponse)
async def create_student(
    payload: StudentCreateRequest, viewer: Annotated[Principal, PersonnelCreateDep]
) -> StudentResponse:
    try:
        created = await service.create_student(
            viewer,
            login_id=payload.login_id,
            student_no=payload.student_no,
            name=payload.name,
            gender=payload.gender,
            class_id=payload.class_id,
            email=payload.email,
            id_card_number=payload.id_card_number,
        )
    except OrgError as exc:
        raise _http_error(exc) from exc
    except IdCardNotConfigured as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    item = created.item
    if not isinstance(item, service.StudentItem):  # pragma: no cover - 类型收窄兜底
        raise RuntimeError("学生建档返回了教师档案类型")
    return StudentResponse(
        id=item.id,
        user_id=item.user_id,
        login_id=item.login_id,
        student_no=item.student_no,
        name=item.name,
        gender=item.gender,
        class_id=item.class_id,
        initial_password=created.initial_password,
    )


@router.get("/students", response_model=PageResponse[StudentResponse])
async def list_students(
    viewer: Annotated[Principal, ViewerDep],
    limit: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> PageResponse[StudentResponse]:
    """学生列表：school 全量 / class 班内学生 / self 本人（fail-closed 空集）。"""

    return await _paged(
        service.list_students(viewer, limit=limit, cursor=cursor),
        StudentResponse,
        hide_initial_password=True,
    )


# ---------------------------------------------------------------------------
# 任教关系
# ---------------------------------------------------------------------------


@router.post("/teacher-assignments", status_code=201, response_model=AssignmentResponse)
async def create_assignment(
    payload: AssignmentCreateRequest, viewer: Annotated[Principal, OrgManageDep]
) -> AssignmentResponse:
    try:
        item = await service.create_assignment(
            viewer,
            teacher_id=payload.teacher_id,
            class_id=payload.class_id,
            course_id=payload.course_id,
            term=payload.term,
        )
    except OrgError as exc:
        raise _http_error(exc) from exc
    return AssignmentResponse(
        id=item.id,
        teacher_id=item.teacher_id,
        class_id=item.class_id,
        course_id=item.course_id,
        term=item.term,
    )


@router.get("/teacher-assignments", response_model=PageResponse[AssignmentResponse])
async def list_assignments(
    viewer: Annotated[Principal, ViewerDep],
    limit: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    teacher_id: Annotated[UUID | None, Query()] = None,
) -> PageResponse[AssignmentResponse]:
    return await _paged(
        service.list_assignments(viewer, limit=limit, cursor=cursor, teacher_id=teacher_id),
        AssignmentResponse,
    )


@router.delete("/teacher-assignments/{assignment_id}", status_code=204)
async def delete_assignment(
    assignment_id: UUID, viewer: Annotated[Principal, OrgManageDep]
) -> None:
    try:
        await service.delete_assignment(viewer, assignment_id)
    except OrgError as exc:
        raise _http_error(exc) from exc


# ---------------------------------------------------------------------------
# 部门/科室树（M2-ORG05；D1 id 物化路径 / D4 叶子+无归属才可硬删）
# ---------------------------------------------------------------------------


class DepartmentResponse(BaseModel):
    id: UUID
    parent_id: UUID | None
    name: str
    code: str | None
    path: str
    status: str


class DepartmentCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    code: str | None = Field(default=None, max_length=32)
    parent_id: UUID | None = None


class DepartmentUpdateRequest(BaseModel):
    """字段级 PUT：缺省=不改；parent_id 显式传 null/值 = 移动。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    code: str | None = Field(default=None, max_length=32)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    parent_id: UUID | None = None


class TeacherUpdateRequest(BaseModel):
    """教师编辑（D2-A）：保持 ORG-05 调岗契约（department_id 必填可空），身份证可选更新。"""

    department_id: UUID | None
    id_card_number: str | None = Field(default=None, max_length=20)


class StudentUpdateRequest(BaseModel):
    """学生编辑（D2-A）：本点仅承接 id_card_number 可选更新。"""

    id_card_number: str | None = Field(default=None, max_length=20)


class StudentTransferRequest(BaseModel):
    """学生转班（ORG-06）：class_id=null 转为未分班。"""

    class_id: UUID | None


class IdCardMaskResponse(BaseModel):
    mask: str


class IdCardFullResponse(BaseModel):
    number: str
    mask: str


class IdCardReadRequest(BaseModel):
    """受控查看理由（D3-A：放请求体，避免 URL/访问日志扩散）。"""

    reason: str = Field(min_length=10, max_length=200)


def _department_response(item: Any) -> DepartmentResponse:
    return DepartmentResponse(
        id=item.id,
        parent_id=item.parent_id,
        name=item.name,
        code=item.code,
        path=item.path,
        status=item.status,
    )


@router.get("/departments", response_model=list[DepartmentResponse])
async def list_departments(
    viewer: Annotated[Principal, ViewerDep],
) -> list[DepartmentResponse]:
    """整棵部门树（扁平 + parent_id；校级规模一次拉全量，无 cursor——简报偏差已记录）。"""

    try:
        items = await departments_service.list_departments(viewer)
    except DepartmentError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    return [_department_response(item) for item in items]


@router.post("/departments", status_code=201, response_model=DepartmentResponse)
async def create_department(
    payload: DepartmentCreateRequest, viewer: Annotated[Principal, OrgManageDep]
) -> DepartmentResponse:
    try:
        item = await departments_service.create_department(
            viewer, name=payload.name, code=payload.code, parent_id=payload.parent_id
        )
    except DepartmentError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    return _department_response(item)


@router.put("/departments/{department_id}", response_model=DepartmentResponse)
async def update_department(
    department_id: UUID,
    payload: DepartmentUpdateRequest,
    viewer: Annotated[Principal, OrgManageDep],
) -> DepartmentResponse:
    kwargs: dict[str, Any] = {}
    for field_name in ("name", "code", "status", "parent_id"):
        if field_name in payload.model_fields_set:
            kwargs[field_name] = getattr(payload, field_name)
    try:
        item = await departments_service.update_department(viewer, department_id, **kwargs)
    except DepartmentError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    return _department_response(item)


@router.delete("/departments/{department_id}", status_code=204)
async def delete_department(
    department_id: UUID, viewer: Annotated[Principal, OrgManageDep]
) -> None:
    try:
        await departments_service.delete_department(viewer, department_id)
    except DepartmentError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc


@router.put("/teachers/{teacher_id}", response_model=TeacherResponse)
async def update_teacher(
    teacher_id: UUID,
    payload: TeacherUpdateRequest,
    viewer: Annotated[Principal, OrgManageDep],
) -> TeacherResponse:
    """教师编辑（ORG-05 调岗 + ORG-04 身份证可选更新）：审计变更前后值（部门名口径）。

    已知限制（TD-179）：调岗与身份证更新分两步提交，身份证步失败时调岗已生效。
    """

    try:
        item = await departments_service.update_teacher_department(
            viewer, teacher_id, department_id=payload.department_id
        )
    except DepartmentError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    if payload.id_card_number is not None:
        try:
            await id_card_service.set_teacher_id_card(
                viewer, teacher_id, number=payload.id_card_number
            )
        except OrgError as exc:
            raise _http_error(exc) from exc
        except IdCardNotConfigured as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
    return TeacherResponse(
        id=item.id,
        user_id=item.user_id,
        login_id=item.login_id,
        employee_no=item.employee_no,
        name=item.name,
        age=item.age,
        gender=item.gender,
        position=item.position,
        initial_password=None,
    )


@router.put("/students/{student_id}", response_model=IdCardMaskResponse)
async def update_student(
    student_id: UUID,
    payload: StudentUpdateRequest,
    viewer: Annotated[Principal, OrgManageDep],
) -> IdCardMaskResponse:
    """学生编辑（D2-A）：本点仅身份证可选更新，返回掩码。"""

    if payload.id_card_number is None:
        raise HTTPException(status_code=422, detail="无可更新字段")
    try:
        mask = await id_card_service.set_student_id_card(
            viewer, student_id, number=payload.id_card_number
        )
    except OrgError as exc:
        raise _http_error(exc) from exc
    except IdCardNotConfigured as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    return IdCardMaskResponse(mask=mask)


@router.post("/students/{student_id}/transfer", response_model=StudentResponse)
async def transfer_student(
    student_id: UUID,
    payload: StudentTransferRequest,
    viewer: Annotated[Principal, OrgManageDep],
) -> StudentResponse:
    """学生转班（ORG-06）：留级/跳级/分班=重指派到目标年级的班；null=转为未分班。"""

    try:
        item = await service.update_student_class(viewer, student_id, class_id=payload.class_id)
    except OrgError as exc:
        raise _http_error(exc) from exc
    return StudentResponse(
        id=item.id,
        user_id=item.user_id,
        login_id=item.login_id,
        student_no=item.student_no,
        name=item.name,
        gender=item.gender,
        class_id=item.class_id,
        initial_password=None,
    )


SensitiveDep = require_permission("school.personnel.sensitive.read", realm="tenant")


@router.get(
    "/personnel/{personnel_id}/id-card",
    response_model=IdCardFullResponse,
)
async def read_id_card(
    personnel_id: UUID,
    payload: IdCardReadRequest,
    viewer: Annotated[Principal, SensitiveDep],
    response: Response,
) -> IdCardFullResponse:
    """受控完整查看（M2-ORG04 D3）：sensitive.read + 请求体理由 → 解密明文 + 审计。

    reason 放请求体（D3-A）：避免自由文本进 URL/访问日志。
    """

    if viewer.tenant_id is None or viewer.realm != "tenant":
        raise HTTPException(status_code=403, detail="仅校方租户上下文可查看")

    view: id_card_service.IdCardView | None = None
    last_error: OrgError | None = None
    for reader in (
        id_card_service.read_teacher_id_card,
        id_card_service.read_student_id_card,
    ):
        try:
            view = await reader(viewer, personnel_id, reason=payload.reason)
            break
        except OrgError as exc:
            if exc.http_status == 404:
                last_error = exc
                continue
            raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
        except IdCardNotConfigured as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc

    if view is None:
        detail = last_error.message if last_error is not None else "人员或身份证不存在"
        raise HTTPException(status_code=404, detail=detail)

    response.headers["Cache-Control"] = "no-store"
    return IdCardFullResponse(number=view.number, mask=view.mask)
