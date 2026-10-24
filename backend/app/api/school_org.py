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

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.principal import Principal, require_permission
from app.db.repository import CursorError
from app.org import departments as departments_service
from app.org import service
from app.org.departments import DepartmentError
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
    grade: str
    name: str
    head_teacher_id: UUID | None
    counselor_id: UUID | None


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
    grade: str = Field(min_length=1, max_length=16)
    name: str = Field(min_length=1, max_length=64)
    head_teacher_id: UUID | None = None
    counselor_id: UUID | None = None


class ClassUpdateRequest(BaseModel):
    """PUT 语义：字段缺省=不改；显式传 null=清空班主任/辅导员。"""

    grade: str | None = Field(default=None, min_length=1, max_length=16)
    name: str | None = Field(default=None, min_length=1, max_length=64)
    head_teacher_id: UUID | None = None
    counselor_id: UUID | None = None


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


class StudentCreateRequest(BaseModel):
    login_id: str = Field(min_length=1, max_length=254)
    student_no: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    gender: str | None = Field(default=None, pattern="^(male|female|other)$")
    class_id: UUID | None = None
    email: str | None = Field(default=None, max_length=254)


class AssignmentCreateRequest(BaseModel):
    teacher_id: UUID
    class_id: UUID
    course_id: UUID
    term: str = Field(min_length=1, max_length=16)


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
            grade=payload.grade,
            name=payload.name,
            head_teacher_id=payload.head_teacher_id,
            counselor_id=payload.counselor_id,
        )
    except OrgError as exc:
        raise _http_error(exc) from exc
    return ClassResponse(
        id=item.id, grade=item.grade, name=item.name,
        head_teacher_id=item.head_teacher_id, counselor_id=item.counselor_id,
    )


@router.get("/classes", response_model=PageResponse[ClassResponse])
async def list_classes(
    viewer: Annotated[Principal, ViewerDep],
    limit: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> PageResponse[ClassResponse]:
    return await _paged(service.list_classes(viewer, limit=limit, cursor=cursor), ClassResponse)


@router.put("/classes/{class_id}", response_model=ClassResponse)
async def update_class(
    class_id: UUID,
    payload: ClassUpdateRequest,
    viewer: Annotated[Principal, OrgManageDep],
) -> ClassResponse:
    """字段级 PUT：只更新请求体里出现的字段（缺省=不改；null=清空指派）。"""

    kwargs: dict[str, Any] = {}
    provided = payload.model_fields_set
    for field_name in ("grade", "name", "head_teacher_id", "counselor_id"):
        if field_name in provided:
            kwargs[field_name] = getattr(payload, field_name)
    try:
        item = await service.update_class(viewer, class_id, **kwargs)
    except OrgError as exc:
        raise _http_error(exc) from exc
    return ClassResponse(
        id=item.id, grade=item.grade, name=item.name,
        head_teacher_id=item.head_teacher_id, counselor_id=item.counselor_id,
    )


@router.delete("/classes/{class_id}", status_code=204)
async def delete_class(
    class_id: UUID, viewer: Annotated[Principal, OrgManageDep]
) -> None:
    try:
        await service.delete_class(viewer, class_id)
    except OrgError as exc:
        raise _http_error(exc) from exc


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
        )
    except OrgError as exc:
        raise _http_error(exc) from exc
    item = created.item
    if not isinstance(item, service.TeacherItem):  # pragma: no cover - 类型收窄兜底
        raise RuntimeError("教师建档返回了学生档案类型")
    return TeacherResponse(
        id=item.id, user_id=item.user_id, login_id=item.login_id,
        employee_no=item.employee_no, name=item.name, age=item.age,
        gender=item.gender, position=item.position,
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
        )
    except OrgError as exc:
        raise _http_error(exc) from exc
    item = created.item
    if not isinstance(item, service.StudentItem):  # pragma: no cover - 类型收窄兜底
        raise RuntimeError("学生建档返回了教师档案类型")
    return StudentResponse(
        id=item.id, user_id=item.user_id, login_id=item.login_id,
        student_no=item.student_no, name=item.name, gender=item.gender,
        class_id=item.class_id, initial_password=created.initial_password,
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
        id=item.id, teacher_id=item.teacher_id, class_id=item.class_id,
        course_id=item.course_id, term=item.term,
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


class TeacherDepartmentUpdateRequest(BaseModel):
    """教师调岗：department_id=null = 取消归属。"""

    department_id: UUID | None


def _department_response(item: Any) -> DepartmentResponse:
    return DepartmentResponse(
        id=item.id, parent_id=item.parent_id, name=item.name,
        code=item.code, path=item.path, status=item.status,
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
        item = await departments_service.update_department(
            viewer, department_id, **kwargs
        )
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
async def update_teacher_department(
    teacher_id: UUID,
    payload: TeacherDepartmentUpdateRequest,
    viewer: Annotated[Principal, OrgManageDep],
) -> TeacherResponse:
    """教师调岗（ORG-05）：审计变更前后值（部门名口径）。"""

    try:
        item = await departments_service.update_teacher_department(
            viewer, teacher_id, department_id=payload.department_id
        )
    except DepartmentError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    return TeacherResponse(
        id=item.id, user_id=item.user_id, login_id=item.login_id,
        employee_no=item.employee_no, name=item.name, age=item.age,
        gender=item.gender, position=item.position, initial_password=None,
    )
