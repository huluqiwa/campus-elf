"""class 数据范围行的同步与对账（M2-ORG01-03 决策 D1 的核心）。

口径（简报 §2-D1）：班级可见性**不实时 join 关系表**，而是把「任教 ∪ 班主任 ∪ 辅导员」
推导出的期望范围行，与 `data_scopes` 里的实际行做差量同步——**调用方必须在组织关系
变更的同一事务里调用 `sync_class_scope_rows`**，保证关系与范围行同回滚。

fail-closed 语义不变：解析不到任何范围行 = 空集（`app/rbac/scopes.py`）。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.org import OrgClass, Student, Teacher, TeacherClassCourse
from app.db.models.rbac import DataScope

_SCOPE_TYPE = "class"


@dataclass(frozen=True, slots=True)
class ScopeSyncResult:
    """一次同步的差量（供测试与审计断言）。"""

    added: int
    removed: int


def scope_row_diff(
    expected: set[tuple[str, str]], actual: set[tuple[str, str]]
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """差量：期望有而实际无 → 增；实际有而期望无 → 删。纯函数（单元测试覆盖）。"""

    return expected - actual, actual - expected


async def expected_class_scope_pairs(
    session: AsyncSession, tenant_id: UUID
) -> set[tuple[str, str]]:
    """从组织关系推导「应有」的 (user_id, class_id) 范围对（字符串形，便于比对）。"""

    result = await session.execute(
        select(Teacher.user_id, TeacherClassCourse.class_id)
        .join(Teacher, TeacherClassCourse.teacher_id == Teacher.id)
        .where(TeacherClassCourse.tenant_id == tenant_id)
    )
    pairs: set[tuple[str, str]] = {
        (str(user_id), str(class_id)) for user_id, class_id in result.all()
    }
    heads = await session.execute(
        select(OrgClass.head_teacher_id, OrgClass.id).where(
            OrgClass.tenant_id == tenant_id, OrgClass.head_teacher_id.is_not(None)
        )
    )
    pairs |= {(str(user_id), str(class_id)) for user_id, class_id in heads.all()}
    counselors = await session.execute(
        select(OrgClass.counselor_id, OrgClass.id).where(
            OrgClass.tenant_id == tenant_id, OrgClass.counselor_id.is_not(None)
        )
    )
    pairs |= {(str(user_id), str(class_id)) for user_id, class_id in counselors.all()}
    return pairs


async def actual_class_scope_pairs(session: AsyncSession, tenant_id: UUID) -> set[tuple[str, str]]:
    """读当前实际的用户级 class 范围行。"""

    rows = (
        await session.execute(
            text(
                "select user_id::text, scope_id::text from data_scopes "
                "where tenant_id = :tenant_id and scope_type = :scope_type "
                "and user_id is not null"
            ),
            {"tenant_id": str(tenant_id), "scope_type": _SCOPE_TYPE},
        )
    ).all()
    return {(str(user_id), str(class_id)) for user_id, class_id in rows}


async def sync_class_scope_rows(session: AsyncSession, tenant_id: UUID) -> ScopeSyncResult:
    """把实际范围行对齐到组织关系推导的期望（必须在关系变更的同一事务内调用）。

    幂等：期望与实际一致时零写入。部分唯一索引
    `uq_data_scopes_user_class`（迁移 c9d5f1e3a7b9）是并发下的最后防线。
    """

    expected = await expected_class_scope_pairs(session, tenant_id)
    actual = await actual_class_scope_pairs(session, tenant_id)
    to_add, to_remove = scope_row_diff(expected, actual)

    for user_id_text, class_id_text in to_add:
        session.add(
            DataScope(
                tenant_id=tenant_id,
                user_id=UUID(user_id_text),
                scope_type=_SCOPE_TYPE,
                scope_id=UUID(class_id_text),
            )
        )
    for user_id_text, class_id_text in to_remove:
        await session.execute(
            text(
                "delete from data_scopes where tenant_id = :tenant_id and scope_type = :scope_type "
                "and user_id = :user_id and scope_id = :scope_id"
            ),
            {
                "tenant_id": str(tenant_id),
                "scope_type": _SCOPE_TYPE,
                "user_id": user_id_text,
                "scope_id": class_id_text,
            },
        )
    return ScopeSyncResult(added=len(to_add), removed=len(to_remove))


def build_user_class_member_factory(
    tenant_id: UUID,
) -> Callable[[frozenset[UUID]], Select[Any]]:
    """给 User 模型用的 class 成员子查询工厂：我的班级里的**学生** user_id 集合。

    班级内的其他教师不在其中（教师互见不是组织需求，人员列表由 school 范围承担）。
    """

    def factory(class_ids: frozenset[UUID]) -> Select[Any]:
        return (
            select(Student.user_id)
            .where(Student.tenant_id == tenant_id, Student.class_id.in_(class_ids))
        )

    return factory
