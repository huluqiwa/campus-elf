"""人群解析器的仓储数据源（KB-09B；协议定义在 `app/kb/audience.py`）。

全部查询走 `TenantScopedRepository`（bare-SQL 守卫口径，KB-09A B1 先例；
TD-317 的 `select_from` 盲区不作为绕过理由）；解析是身份侧系统判据（与
`Principal` 每请求解析同性质），按 ScopeBypass 显式留痕、tenant_id 由仓储与
RLS 双重定界。实时解析无缓存（TD-098 缓存归 M4 chat）。

口径（简报 `docs/design-briefs/M3-KB-09.md` §3.2.1，决策卡 B1）：
- 教师 = `teachers.department_id`（可空=未归属）+ 祖先链（departments.path
  前缀段集，在纯函数层切分）+ `user_roles` + 任教/班主任班级 + 授课课程；
- 学生 = `students.class_id`（可空=未分班）+ 班级现值年级；
- school_admin 无档案表 → 仅角色；users 行不存在 → 上层空集 fail-closed。
- class/course 不按 status/term 过滤（与配置侧存在性校验口径一致，简报 §3.2.1
  显式假设 ①）；辅导员（classes.counselor_id）不在人群范围（显式假设 ②）。
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.identity import User
from app.db.models.org import Department, OrgClass, Student, Teacher, TeacherClassCourse
from app.db.models.rbac import UserRole
from app.db.repository import ScopeBypass, TenantScopedRepository
from app.kb.audience import StudentFacts, TeacherFacts

_REASON = "人群解析：按用户实时解析组织关系，系统判据"


class RepositoryAudienceSource:
    """`AudienceSource` 协议的仓储实装（会话由调用方事务管理）。"""

    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _repo(self, model: type) -> TenantScopedRepository[Any]:
        return TenantScopedRepository(
            self._session, model, self._tenant_id, scope=ScopeBypass(reason=_REASON)
        )

    async def load_user(self, user_id: UUID) -> User | None:
        """users 行（含 user_type/status；不存在或平台 realm → None）。"""

        return await self._repo(User).get(user_id)

    async def load_user_type(self, user_id: UUID) -> str | None:
        user = await self.load_user(user_id)
        return user.user_type if user is not None else None

    async def load_teacher_facts(self, user_id: UUID) -> TeacherFacts | None:
        teacher_rows = (
            (
                await self._session.execute(
                    self._repo(Teacher).base_query().where(Teacher.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        teacher = teacher_rows[0] if teacher_rows else None
        department_path: str | None = None
        taught: set[UUID] = set()
        courses: set[UUID] = set()
        if teacher is not None:
            if teacher.department_id is not None:
                department = await self._repo(Department).get(teacher.department_id)
                department_path = department.path if department is not None else None
            tcc_rows = (
                (
                    await self._session.execute(
                        self._repo(TeacherClassCourse).base_query().where(
                            TeacherClassCourse.teacher_id == teacher.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in tcc_rows:
                taught.add(row.class_id)
                courses.add(row.course_id)
        # 班主任权威在 classes.head_teacher_id（FK 是 users.id，不依赖档案行）——
        # 无 teachers 行的班主任同样命中班级人群
        head_rows = (
            (
                await self._session.execute(
                    self._repo(OrgClass).base_query().where(OrgClass.head_teacher_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        return TeacherFacts(
            department_path=department_path,
            taught_class_ids=tuple(sorted(taught)),
            course_ids=tuple(sorted(courses)),
            head_class_ids=tuple(sorted(row.id for row in head_rows)),
        )

    async def load_student_facts(self, user_id: UUID) -> StudentFacts | None:
        student_rows = (
            (
                await self._session.execute(
                    self._repo(Student).base_query().where(Student.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        if not student_rows:
            return None
        student = student_rows[0]
        grade: str | None = None
        if student.class_id is not None:
            klass = await self._repo(OrgClass).get(student.class_id)
            grade = klass.grade if klass is not None else None
        return StudentFacts(class_id=student.class_id, grade=grade)

    async def load_role_ids(self, user_id: UUID) -> tuple[UUID, ...]:
        rows_ = (
            (
                await self._session.execute(
                    self._repo(UserRole).base_query().where(UserRole.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        return tuple(sorted(row.role_id for row in rows_))
