"""部门/科室树服务（M2 ORG-05）。

口径（`docs/design-briefs/M2-ORG05-departments.md`，已确认）：
- **D1**：邻接表 + id 物化路径（`path` 以自身 id 结尾）；子树查询 `path LIKE '{自身 path}/%'`；
  移动子树在**同一事务**内重写整棵子树的 path；
- **D2**：教师单一部门归属（`teachers.department_id` 可空 = 未归属）；
- **D4**：硬删仅允许叶子且无在职教师归属；停用不受限但不可接新归属/新调岗。

调岗（`update_teacher_department`）不影响 class 范围行（班级范围来自任教关系，
与部门正交——TD-174 对账不受影响）；「人群按新部门生效」无应用层缓存需失效
（M3 KB-09 人群判定每请求查库）。
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.imports import ImportJob  # noqa: F401  (重试行过滤引用)
from app.db.models.org import Department, Teacher
from app.db.repository import ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.org.service import (
    UNSET,
    OrgActor,
    TeacherItem,
    _audit,
    _require_school_scope,
    _require_tenant,
)

# 探针/系统执行者理由（组织结构非个人数据；导入任务为系统执行者）
_DEPT_PROBE_REASON = "部门树操作的存在性/状态核验：只读主键与状态位"
_DEPT_SYSTEM_REASON = "导入任务解析目标部门：部门目录非个人数据"


class DepartmentError(RuntimeError):
    """部门树的业务错误（映射为 HTTP 状态码）。"""

    def __init__(
        self, code: str, message: str, http_status: int, *, field: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.field = field


@dataclass(frozen=True, slots=True)
class DepartmentItem:
    id: UUID
    parent_id: UUID | None
    name: str
    code: str | None
    path: str
    status: str


def _department_repository(
    session: AsyncSession, tenant_id: UUID, scope: Any
) -> TenantScopedRepository[Department]:
    return TenantScopedRepository(session, Department, tenant_id, scope=scope)


def _department_item(department: Department) -> DepartmentItem:
    return DepartmentItem(
        id=department.id, parent_id=department.parent_id, name=department.name,
        code=department.code, path=department.path, status=department.status,
    )


def _path_of(parent_path: str | None, own_id: UUID) -> str:
    """id 物化路径：根为 `/{id}`，子为 `{父 path}/{id}`（以自身 id 结尾）。"""

    return f"{parent_path}/{own_id}" if parent_path else f"/{own_id}"


async def _assert_descendant_free(
    session: AsyncSession, tenant_id: UUID, new_parent_id: UUID | None, own_id: UUID
) -> None:
    """移动校验：沿新父的 parent_id 链上溯，遇到自身即成环。

    不能用 path 判断：path 在移动事务内会被重写，旧祖先 id 不再出现于新父 path。
    """

    repository: TenantScopedRepository[Department] = _department_repository(
        session, tenant_id, ScopeBypass(reason=_DEPT_PROBE_REASON)
    )
    current: UUID | None = new_parent_id
    while current is not None:
        if current == own_id:
            raise DepartmentError(
                "DEPARTMENT_CYCLE", "不能把部门移动到它自己或它的子部门下", 422
            )
        department = await repository.get(current)
        if department is None:  # pragma: no cover - 父链由复合外键保证
            break
        current = department.parent_id


async def _get_department(
    session: AsyncSession, tenant_id: UUID, department_id: UUID
) -> Department | None:
    repository: TenantScopedRepository[Department] = _department_repository(
        session, tenant_id, ScopeBypass(reason=_DEPT_PROBE_REASON)
    )
    return await repository.get(department_id)


def _validate_department_name(name: str) -> None:
    """名称不得含 `/`（D3 名称全路径的前提）且不得首尾带空白。"""

    if name != name.strip() or "/" in name:
        raise DepartmentError(
            "NAME_INVALID", "部门名称不能包含 / 且首尾不能有空白", 422, field="name"
        )


def _assert_path_length(path: str) -> None:
    """物化路径长度校验（列宽 600，约 16 层；校级规模远不可达，防御 500）。"""

    if len(path) > 600:
        raise DepartmentError("DEPARTMENT_TOO_DEEP", "部门层级过深", 422)


async def create_department(
    principal: OrgActor, *, name: str, code: str | None, parent_id: UUID | None
) -> DepartmentItem:
    tenant_id = _require_tenant(principal)
    _validate_department_name(name)
    department_id = uuid4()
    async with db_session(tenant_id=tenant_id) as session:
        parent_path: str | None = None
        if parent_id is not None:
            parent = await _get_department(session, tenant_id, parent_id)
            if parent is None:
                raise DepartmentError("DEPARTMENT_NOT_FOUND", "父部门不存在", 422)
            if parent.status != "active":
                raise DepartmentError("PARENT_DISABLED", "父部门已停用，不能挂子部门", 422)
            parent_path = parent.path
        department = Department(
            id=department_id, tenant_id=tenant_id, parent_id=parent_id,
            name=name, code=code,
            path=_path_of(parent_path, department_id), status="active",
        )
        _assert_path_length(department.path)
        # 同父同名唯一由 uq_departments_tenant_id_parent_id_name / 根部分唯一索引兜底
        session.add(department)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise DepartmentError(
                "DEPARTMENT_CONFLICT", "同级下已存在同名部门", 409, field="name"
            ) from exc
        await _audit(
            session, principal, tenant_id,
            action="org.department.created", resource_type="org_department",
            resource_id=str(department.id),
            metadata={"name": name, "code": code,
                      "parent_id": str(parent_id) if parent_id else None,
                      "path": department.path},
        )
        return _department_item(department)


async def update_department(
    principal: OrgActor, department_id: UUID, *,
    name: Any = UNSET,
    code: Any = UNSET,
    status: Any = UNSET,
    parent_id: Any = UNSET,
) -> DepartmentItem:
    """更新部门；显式传 parent_id 触发移动（子树 path 同事务重写）。"""

    kwargs: dict[str, Any] = {
        "name": name, "code": code, "status": status, "parent_id": parent_id,
    }
    provided: set[str] = {k for k, v in kwargs.items() if v is not UNSET}
    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        repository: TenantScopedRepository[Department] = _department_repository(
            session, tenant_id, principal.scope_context
        )
        department = await repository.get(department_id, with_for_update=True)
        if department is None:
            raise DepartmentError("DEPARTMENT_NOT_FOUND", "部门不存在", 404)

        if "name" in provided and kwargs["name"] is None:
            raise DepartmentError("NAME_REQUIRED", "部门名称不能为空", 422, field="name")
        if "status" in provided and kwargs["status"] is None:
            raise DepartmentError("STATUS_REQUIRED", "部门状态不能为空", 422, field="status")

        before_name: str = department.name
        before_code: str | None = department.code
        before_status: str = department.status
        before_parent: UUID | None = department.parent_id
        changed: list[str] = []

        if name is not UNSET:
            _validate_department_name(name)
            if name != department.name:
                department.name = name
                changed.append("name")
        if code is not UNSET and code != department.code:
            department.code = code
            changed.append("code")
        if status is not UNSET and status != department.status:
            department.status = status
            changed.append("status")

        move_requested = parent_id is not UNSET and parent_id != department.parent_id
        if move_requested:
            new_parent_id: UUID | None = parent_id
            if new_parent_id is not None:
                new_parent = await repository.get(new_parent_id)
                if new_parent is None:
                    raise DepartmentError("DEPARTMENT_NOT_FOUND", "目标父部门不存在", 422)
                if new_parent.status != "active":
                    raise DepartmentError("PARENT_DISABLED", "目标父部门已停用", 422)
                await _assert_descendant_free(
                    session, tenant_id, new_parent_id, department.id
                )
                new_path = _path_of(new_parent.path, department.id)
            else:
                new_path = _path_of(None, department.id)

            old_path = department.path
            department.parent_id = new_parent_id
            department.path = new_path
            _assert_path_length(new_path)
            changed.append("parent_id")
            # 子树重写：后代 path 前缀从旧前缀替换为新前缀（同一事务，漏改=层级包含错判）
            await session.execute(
                text(
                    "update departments set path = :new_base || substring(path from cast(:old_len as int)) "
                    "where tenant_id = :tenant_id and path like :old_prefix"
                ),
                {
                    # substring 是 1 基：跳过旧前缀本身 + 其后的分隔符，需 len+2
                    "new_base": f"{new_path}/", "old_len": len(old_path) + 2,
                    "tenant_id": str(tenant_id), "old_prefix": f"{old_path}/%",
                },
            )

        if not changed:
            return _department_item(department)

        try:
            await session.flush()
        except IntegrityError as exc:
            raise DepartmentError(
                "DEPARTMENT_CONFLICT", "同级下已存在同名部门", 409, field="name"
            ) from exc

        await _audit(
            session, principal, tenant_id,
            action="org.department.updated", resource_type="org_department",
            resource_id=str(department_id),
            metadata={
                "changed": changed,
                "before": {"name": before_name, "code": before_code,
                           "status": before_status, "parent_id": str(before_parent)
                           if before_parent else None},
                "after": {"name": department.name, "code": department.code,
                          "status": department.status,
                          "parent_id": str(department.parent_id)
                          if department.parent_id else None},
            },
        )
        return _department_item(department)


async def delete_department(principal: OrgActor, department_id: UUID) -> None:
    """硬删：仅叶子（无子部门）且无在职教师归属（D4）。"""

    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        repository: TenantScopedRepository[Department] = _department_repository(
            session, tenant_id, principal.scope_context
        )
        department = await repository.get(department_id)
        if department is None:
            raise DepartmentError("DEPARTMENT_NOT_FOUND", "部门不存在", 404)
        child_count: int = (
            await session.execute(
                Department.__table__.select()
                .with_only_columns(func.count())
                .where(
                    Department.tenant_id == tenant_id,
                    Department.parent_id == department_id,
                )
            )
        ).scalar_one()
        if child_count:
            raise DepartmentError(
                "DEPARTMENT_HAS_CHILDREN", "部门仍有子部门，先删除或移动子部门", 409
            )
        teacher_count: int = (
            await session.execute(
                Teacher.__table__.select()
                .with_only_columns(func.count())
                .where(
                    Teacher.tenant_id == tenant_id,
                    Teacher.department_id == department_id,
                )
            )
        ).scalar_one()
        if teacher_count:
            raise DepartmentError(
                "DEPARTMENT_HAS_TEACHERS", "部门仍有在职教师归属，先调岗后再删除", 409
            )
        await repository.delete(department)
        await _audit(
            session, principal, tenant_id,
            action="org.department.deleted", resource_type="org_department",
            resource_id=str(department_id),
            metadata={"name": department.name, "code": department.code,
                      "path": department.path},
        )


async def list_departments(principal: OrgActor) -> list[DepartmentItem]:
    """整棵树（扁平 + parent_id，前端组树）；校级规模一次拉全量（简报偏差已记录）。"""

    tenant_id = _require_school_scope(principal)
    async with db_session(tenant_id=tenant_id) as session:
        repository: TenantScopedRepository[Department] = _department_repository(
            session, tenant_id, principal.scope_context
        )
        rows = (
            await session.execute(
                repository.base_query().order_by(Department.path.asc())
            )
        ).scalars().all()
        return [_department_item(row) for row in rows]


async def update_teacher_department(
    principal: OrgActor, teacher_id: UUID, *, department_id: UUID | None
) -> TeacherItem:
    """教师调岗（ORG-05）：改部门归属并审计变更前后值（部门名口径）。

    不影响 class 范围行（班级范围来自任教关系，与部门正交）。
    """

    tenant_id = _require_tenant(principal)
    async with db_session(tenant_id=tenant_id) as session:
        teacher_repository: TenantScopedRepository[Teacher] = TenantScopedRepository(
            session, Teacher, tenant_id, scope=principal.scope_context, owner_attr="user_id",
        )
        teacher = await teacher_repository.get(teacher_id)
        if teacher is None:
            raise DepartmentError("TEACHER_NOT_FOUND", "教师不存在", 404)

        department_repository: TenantScopedRepository[Department] = _department_repository(
            session, tenant_id, principal.scope_context
        )
        before_department: str | None = None
        if teacher.department_id is not None:
            before = await department_repository.get(teacher.department_id)
            before_department = before.name if before else None

        new_department: Department | None = None
        if department_id is not None:
            new_department = await department_repository.get(department_id)
            if new_department is None:
                raise DepartmentError("DEPARTMENT_NOT_FOUND", "目标部门不存在", 422)
            if new_department.status != "active":
                raise DepartmentError("DEPARTMENT_DISABLED", "目标部门已停用", 422)

        before_id = teacher.department_id
        if department_id != before_id:
            teacher.department_id = department_id
            after_name = new_department.name if new_department else None
            await _audit(
                session, principal, tenant_id,
                action="org.teacher.updated", resource_type="org_teacher",
                resource_id=str(teacher_id),
                metadata={
                    "changed": ["department_id"],
                    "before": {"department": before_department},
                    "after": {"department": after_name},
                    "employee_no": teacher.employee_no, "name": teacher.name,
                },
            )
        user_login: str | None = (
            await session.execute(
                text("select login_id from users where id = :uid"),
                {"uid": str(teacher.user_id)},
            )
        ).scalar_one()
        return TeacherItem(
            id=teacher.id, user_id=teacher.user_id, login_id=str(user_login),
            employee_no=teacher.employee_no, name=teacher.name, age=teacher.age,
            gender=teacher.gender, position=teacher.position,
        )


async def resolve_department_path(
    session: AsyncSession, tenant_id: UUID, path: str
) -> UUID | None:
    """按名称全路径（`院/系/组`，`/` 分层）逐级解析**在用**部门 id。

    未知/中途断链/**命中停用部门** → None（调用方报 DEPARTMENT_NOT_FOUND 错误行；
    M1 复审：停用部门不得经导入获得新归属——D4 停用状态机闭环）；
    同层同名（不同父下）由全路径天然消歧。
    """

    repository: TenantScopedRepository[Department] = _department_repository(
        session, tenant_id, ScopeBypass(reason=_DEPT_SYSTEM_REASON)
    )
    current_parent: UUID | None = None
    for segment in [seg.strip() for seg in path.split("/") if seg.strip()]:
        # IS 仅接受 NULL/布尔：非空父 id 必须用 ==（IS $n 会被 PG 拒绝）
        parent_clause = (
            Department.parent_id.is_(None)
            if current_parent is None
            else Department.parent_id == current_parent
        )
        found = (
            await session.execute(
                repository.base_query().where(
                    parent_clause,
                    Department.name == segment,
                    Department.status == "active",
                )
            )
        ).scalars().first()
        if found is None:
            return None
        current_parent = found.id
    return current_parent
