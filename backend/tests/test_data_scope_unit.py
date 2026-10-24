"""数据范围（RBAC-02）的纯逻辑单元测试：解析、归一与谓词生成。

口径来自 `docs/design-briefs/M1-E2-data-scope.md` §0 ⑨⑩：
`school` 覆盖全校；`self` 只看本人；**解析不到任何范围行 = 空集**（fail-closed，
不回退成全校）；本期只实现 `school`/`self`，`class`/`course` 不得被静默当成全校。
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.models.identity import User
from app.rbac.scopes import (
    DataScopeContext,
    scope_context_from_rows,
    scope_predicate,
)


class _Base(DeclarativeBase):
    """测试内私有模型基类（只为造一张带归属列的表）。"""


class _LeaveRequest(_Base):
    __tablename__ = "leave_requests"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column()
    applicant_user_id: Mapped[UUID] = mapped_column()


def _sql(stmt: Any) -> str:
    """渲染 SQL（字面量内联），供文本断言。"""

    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _has_uuid(sql: str, value: UUID) -> bool:
    """SQL 文本里是否含该 UUID（方言可能渲染成带/不带连字符两种形态）。"""

    return str(value) in sql or str(value).replace("-", "") in sql


def test_school_scope_is_all_visible() -> None:
    """全校范围不追加归属谓词（只剩租户过滤）。"""

    context = DataScopeContext(subject_user_id=uuid4(), scope_types=frozenset({"school", "self"}))
    predicate = scope_predicate(User, context, owner_attr="id")

    sql = _sql(select(User.id).where(predicate))
    assert "users.id =" not in sql
    assert "true" in sql.lower()


def test_self_scope_pins_owner_column_to_subject() -> None:
    """只有 self 时，谓词必须把归属列钉在当前主体上。"""

    subject = uuid4()
    context = DataScopeContext(subject_user_id=subject, scope_types=frozenset({"self"}))
    predicate = scope_predicate(_LeaveRequest, context, owner_attr="applicant_user_id")

    sql = _sql(select(_LeaveRequest.id).where(predicate))
    assert "applicant_user_id =" in sql
    assert _has_uuid(sql, subject)


def test_empty_scope_denies_everything() -> None:
    """解析不到范围行 ⇒ false()：列表为空、详情查不到（不回退成全校）。"""

    context = DataScopeContext(subject_user_id=uuid4(), scope_types=frozenset())
    predicate = scope_predicate(User, context, owner_attr="id")

    sql = _sql(select(User.id).where(predicate))
    assert "false" in sql.lower()
    assert "users.id =" not in sql


def test_scope_context_unions_role_and_user_rows() -> None:
    """角色与用户两侧范围取并集；重复行不产生重复范围。"""

    context = scope_context_from_rows(
        subject_user_id=uuid4(),
        rows=[("self", None), ("school", None), ("school", None)],
    )

    assert context.scope_types == frozenset({"school", "self"})


def test_scope_context_from_rows_rejects_unknown_type() -> None:
    """未知 scope_type 必须失败，不能静默忽略后当成全校放行。"""

    with pytest.raises(ValueError, match="未知"):
        scope_context_from_rows(subject_user_id=uuid4(), rows=[("warehouse", None)])


def test_class_scope_without_factory_does_not_widen_visibility() -> None:
    """class 范围在**没有成员子查询工厂**的模型上不贡献可见性（fail-closed，绝不放大成全校）。

    M2-ORG01-03 起实现 class 过滤，但只在提供了工厂的模型生效（如人员/学生列表）；
    其他模型拿到 class 范围仍只能按 self 收窄或拒绝。
    """

    context = scope_context_from_rows(
        subject_user_id=uuid4(), rows=[("class", str(uuid4())), ("self", None)]
    )

    sql = _sql(select(User.id).where(scope_predicate(User, context, owner_attr="id")))
    assert "users.id =" in sql  # 没有工厂：class 不贡献可见性，仍按 self 收窄


def test_class_only_scope_denies_without_factory() -> None:
    """只有 class 范围且无工厂时不可见任何行：不得退化成全校。"""

    context = scope_context_from_rows(subject_user_id=uuid4(), rows=[("class", str(uuid4()))])

    sql = _sql(select(User.id).where(scope_predicate(User, context, owner_attr="id")))
    assert "false" in sql.lower()


def test_class_scope_with_factory_narrows_to_members() -> None:
    """class 范围 + 成员子查询工厂 → 谓词 = self ∪ 班级成员（M2-ORG01-03 D1）。"""

    member_ids = select(User.id).where(User.id == uuid4())
    context = scope_context_from_rows(
        subject_user_id=uuid4(), rows=[("class", str(uuid4())), ("self", None)]
    )

    sql = _sql(
        select(User.id).where(
            scope_predicate(User, context, owner_attr="id", class_member_subquery=member_ids)
        )
    )
    assert "users.id =" in sql
    assert "IN" in sql.upper()


def test_course_scope_still_does_not_widen_visibility() -> None:
    """course 过滤的业务表在 M3（TD-151）：本期 course 不贡献可见性。"""

    context = scope_context_from_rows(subject_user_id=uuid4(), rows=[("course", str(uuid4()))])

    sql = _sql(select(User.id).where(scope_predicate(User, context, owner_attr="id")))
    assert "false" in sql.lower()


def test_missing_owner_column_fails_at_construction() -> None:
    """表上没有归属列时报错，而不是静默放行整张表。"""

    from app.db.repository import TenantScopedRepository

    class _Session:
        def add(self, obj: object) -> None:  # pragma: no cover - 不会被调用
            raise AssertionError("不应写入")

        async def flush(self) -> None:  # pragma: no cover
            return None

    context = DataScopeContext(subject_user_id=uuid4(), scope_types=frozenset({"self"}))
    with pytest.raises(ValueError, match="归属列"):
        TenantScopedRepository(
            _Session(),  # type: ignore[arg-type]
            User,
            uuid4(),
            scope=context,
            owner_attr="not_a_column",
        )


def test_bypass_must_carry_a_reason() -> None:
    """绕过范围声明必须带理由：空理由视为未声明，禁止「忘了配范围」的静默放行。"""

    from app.db.repository import ScopeBypass

    with pytest.raises(ValueError, match="理由"):
        ScopeBypass(reason="   ")


def test_bypass_query_keeps_tenant_filter_only() -> None:
    """显式绕过（本人自助路径）只加租户过滤，不加范围谓词。"""

    from app.db.repository import ScopeBypass, TenantScopedRepository

    class _Session:
        def add(self, obj: object) -> None:  # pragma: no cover
            raise AssertionError("不应写入")

        async def flush(self) -> None:  # pragma: no cover
            return None

    tenant_id = uuid4()
    repository = TenantScopedRepository(
        _Session(),  # type: ignore[arg-type]
        User,
        tenant_id,
        scope=ScopeBypass(reason="本人自助路径：按 user_id 精确取自己的行"),
    )

    sql = _sql(repository.base_query())
    assert "users.tenant_id" in sql
    assert _has_uuid(sql, tenant_id)
    assert "users.id =" not in sql
