"""租户级仓储基座的单元测试（不依赖数据库）。"""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.models.rbac import Permission
from app.db.models.tenant import PlatformUser, Tenant
from app.db.repository import CrossTenantAccessError, TenantScopedRepository
from app.rbac.scopes import DataScopeContext


def _school_scope() -> DataScopeContext:
    """全校范围（这些用例只验租户过滤，范围本身的用例见 tests/test_data_scope_unit.py）。"""

    return DataScopeContext(subject_user_id=uuid4(), scope_types=frozenset({"school"}))


def _uuid_literal(value: Any) -> str:
    """PG 方言渲染 UUID 字面量时通常去掉连字符，兼容两种写法。"""

    return str(value).replace("-", "")


class _FakeSession:
    """只记录调用痕迹的最小会话替身。"""

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def test_repository_rejects_non_tenant_scoped_model() -> None:
    """非租户级模型禁止使用租户仓储（避免误用造成越权面）。"""

    with pytest.raises(ValueError, match="不是租户级模型"):
        TenantScopedRepository(_FakeSession(), Permission, uuid4(), scope=_school_scope())  # type: ignore[arg-type, type-var]


def test_scoped_query_always_filters_by_tenant() -> None:
    """查询一律追加 tenant_id 条件（SQL 文本可断言）。"""

    from app.db.models.identity import User

    tenant_id = uuid4()
    repository = TenantScopedRepository(_FakeSession(), User, tenant_id, scope=_school_scope())  # type: ignore[arg-type]

    sql = str(repository.base_query().compile(compile_kwargs={"literal_binds": True}))

    assert "users.tenant_id" in sql
    assert _uuid_literal(tenant_id) in sql


def test_get_scopes_by_primary_key_and_tenant() -> None:
    """按主键取单行时同样带租户条件（跨租直接查不到）。"""

    from app.db.models.identity import User

    tenant_id = uuid4()
    object_id = uuid4()
    repository = TenantScopedRepository(_FakeSession(), User, tenant_id, scope=_school_scope())  # type: ignore[arg-type]

    stmt = repository.scoped(select(User).where(User.id == object_id))
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))

    assert "users.id" in sql
    assert "users.tenant_id" in sql
    assert _uuid_literal(object_id) in sql


async def test_add_fills_missing_tenant_id() -> None:
    """新增对象未带 tenant_id 时自动回填当前租户。"""

    from app.db.models.identity import User

    tenant_id = uuid4()
    session = _FakeSession()
    repository = TenantScopedRepository(session, User, tenant_id, scope=_school_scope())  # type: ignore[arg-type]

    user = User(login_id="2024001", user_type="student", password_hash="x")
    await repository.add(user)

    assert user.tenant_id == tenant_id


async def test_add_rejects_foreign_tenant_object() -> None:
    """对象显式带其它租户 tenant_id 时必须拒绝写入。"""

    from app.db.models.identity import User

    repository = TenantScopedRepository(_FakeSession(), User, uuid4(), scope=_school_scope())  # type: ignore[arg-type]
    foreign = User(login_id="2024002", user_type="student", password_hash="x", tenant_id=uuid4())

    with pytest.raises(CrossTenantAccessError, match="拒绝访问"):
        await repository.add(foreign)


def test_assert_same_tenant_detects_mismatch() -> None:
    """跨租对象校验（读路径同样拒绝）。"""

    from app.db.models.identity import User

    tenant_id = uuid4()
    repository = TenantScopedRepository(_FakeSession(), User, tenant_id, scope=_school_scope())  # type: ignore[arg-type]
    other = User(login_id="2024003", user_type="student", password_hash="x", tenant_id=uuid4())

    with pytest.raises(CrossTenantAccessError):
        repository.assert_same_tenant(other)


def test_platform_tables_are_not_tenant_scoped() -> None:
    """平台级模型（Tenant/PlatformUser）不应带 tenant_id（防止被误当作租户表）。"""

    assert not hasattr(Tenant, "tenant_id")
    assert not hasattr(PlatformUser, "tenant_id")
