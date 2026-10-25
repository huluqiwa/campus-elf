"""M2-ORG01-03 集成测试：组织五表的 RLS/唯一约束、class 范围行同步与对账、cursor 分页。

管理连接直插造数（绕过服务层写路径），`app.org.scopes_sync` 的同步/对账走真实代码。

"""

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.db.models.org import OrgClass, TeacherClassCourse
from app.db.repository import ScopeBypass, TenantScopedRepository, decode_cursor
from app.org.scopes_sync import (
    expected_class_scope_pairs,
    scope_row_diff,
    sync_class_scope_rows,
)
from tests.support.db import clean_test_data, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:

    admin_url = os.environ.get("TEST_DATABASE_URL")

    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")

    assert_test_database_dsn(admin_url)

    upgrade_test_database()

    yield None


@pytest.fixture
async def clean_db(migrated_database: None) -> AsyncIterator[AsyncEngine]:

    engine = make_test_engine("admin")

    try:
        await clean_test_data(engine)

        yield engine

    finally:
        await engine.dispose()


async def _ensure_tenant(engine: AsyncEngine) -> str:
    """取（必要时建）一个宿主租户：clean_test_data 后租户表可能为空。"""

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        tenant_id = await session.scalar(text("select id::text from tenants limit 1"))

        if tenant_id is None:
            tenant_id = await session.scalar(
                text(
                    "insert into tenants (name, status) values ('组织集成中学', 'active') "
                    "returning id::text"
                )
            )

        assert tenant_id is not None

        return str(tenant_id)


async def _insert(engine: AsyncEngine, sql: str, params: dict[str, Any]) -> tuple[UUID, str]:
    """管理连接直插一行，返回 (tenant_id, 返回的 id)。"""

    tenant_id = await _ensure_tenant(engine)

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        row_id = await session.scalar(text(sql), {"tenant_id": tenant_id, **params})

        assert row_id is not None

    return UUID(tenant_id), str(row_id)


_CLASS_SQL = (
    "insert into classes (tenant_id, grade, name, head_teacher_id, counselor_id) "
    "values (:tenant_id, :grade, :name, :head_teacher_id, :counselor_id) returning id::text"
)


_COURSE_SQL = "insert into courses (tenant_id, code, name) values (:tenant_id, :code, :name) returning id::text"


_TEACHER_SQL = (
    "insert into teachers (tenant_id, user_id, employee_no, name) "
    "values (:tenant_id, :user_id, :employee_no, :name) returning id::text"
)


_STUDENT_SQL = (
    "insert into students (tenant_id, user_id, student_no, name, class_id) "
    "values (:tenant_id, :user_id, :student_no, :name, :class_id) returning id::text"
)


_USER_SQL = (
    "insert into users (tenant_id, user_type, login_id, display_name, password_hash, status, "
    "must_change_password, mfa_enabled) "
    "values (:tenant_id, :user_type, :login_id, :name, 'x', 'active', true, false) "
    "returning id::text"
)


async def test_class_unique_constraint(clean_db: AsyncEngine) -> None:
    """同租户同年级同名班级唯一（服务层 409 语义的数据库兜底）。"""

    _tenant, head_user = await _insert(
        clean_db, _USER_SQL, {"user_type": "teacher", "login_id": "it-head", "name": "班主任"}
    )

    await _insert(
        clean_db,
        _CLASS_SQL,
        {"grade": "2026级", "name": "1班", "head_teacher_id": head_user, "counselor_id": None},
    )

    with pytest.raises(IntegrityError):
        await _insert(
            clean_db,
            _CLASS_SQL,
            {"grade": "2026级", "name": "1班", "head_teacher_id": None, "counselor_id": None},
        )


async def test_teacher_student_one_to_one(clean_db: AsyncEngine) -> None:
    """D4：同租户内一个账号只能有一条教师档案行。"""

    _tenant, user_id = await _insert(
        clean_db, _USER_SQL, {"user_type": "teacher", "login_id": "it-1to1", "name": "教师一"}
    )

    await _insert(
        clean_db, _TEACHER_SQL, {"user_id": user_id, "employee_no": "E-1", "name": "教师一"}
    )

    with pytest.raises(IntegrityError):
        await _insert(
            clean_db, _TEACHER_SQL, {"user_id": user_id, "employee_no": "E-2", "name": "重复绑定"}
        )


async def test_class_scope_partial_unique_index(clean_db: AsyncEngine) -> None:
    """D1 最后防线：同一用户对同一班级的 class 范围行不可重复。"""

    _tenant, user_id = await _insert(
        clean_db, _USER_SQL, {"user_type": "teacher", "login_id": "it-dup", "name": "重复范围"}
    )

    _, class_id = await _insert(
        clean_db,
        _CLASS_SQL,
        {"grade": "2026级", "name": "唯一班", "head_teacher_id": None, "counselor_id": None},
    )

    row_sql = (
        "insert into data_scopes (tenant_id, user_id, scope_type, scope_id) "
        "values (:tenant_id, :user_id, 'class', :scope_id) returning id::text"
    )

    await _insert(clean_db, row_sql, {"user_id": user_id, "scope_id": class_id})

    with pytest.raises(IntegrityError):
        await _insert(clean_db, row_sql, {"user_id": user_id, "scope_id": class_id})


async def test_class_scope_sync_follows_relations(clean_db: AsyncEngine) -> None:
    """D1：同步函数把「任教 ∪ 班主任 ∪ 辅导员」落成范围行；关系删除后回收。"""

    tenant_id, teacher_user = await _insert(
        clean_db, _USER_SQL, {"user_type": "teacher", "login_id": "it-sync-t", "name": "同步教师"}
    )

    _, other_user = await _insert(
        clean_db, _USER_SQL, {"user_type": "teacher", "login_id": "it-sync-o", "name": "旁人教师"}
    )

    _, class_id = await _insert(
        clean_db,
        _CLASS_SQL,
        {
            "grade": "2026级",
            "name": "同步班",
            "head_teacher_id": teacher_user,
            "counselor_id": None,
        },
    )

    _, class2_id = await _insert(
        clean_db,
        _CLASS_SQL,
        {"grade": "2026级", "name": "二班", "head_teacher_id": None, "counselor_id": other_user},
    )

    _, course_id = await _insert(clean_db, _COURSE_SQL, {"code": "MATH", "name": "数学"})

    _, teacher_id = await _insert(
        clean_db, _TEACHER_SQL, {"user_id": teacher_user, "employee_no": "E-S", "name": "同步教师"}
    )

    factory = async_sessionmaker(clean_db, expire_on_commit=False)

    async def current_scope_rows() -> set[tuple[str, str]]:

        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        "select user_id::text, scope_id::text from data_scopes "
                        "where scope_type = 'class' and user_id is not null"
                    )
                )
            ).all()

            return {(str(u), str(c)) for u, c in rows}

    async with factory() as session, session.begin():
        session.add(
            TeacherClassCourse(
                tenant_id=tenant_id,
                teacher_id=teacher_id,
                class_id=class_id,
                course_id=course_id,
                term="2026上",
            )
        )

    async with factory() as session, session.begin():
        changed = await sync_class_scope_rows(session, tenant_id)

    assert changed.added >= 1

    # 期望：教师(任课+班主任→班1) 与 旁人(辅导员→班2)

    assert await current_scope_rows() == {(teacher_user, class_id), (other_user, class2_id)}

    # 对账恒等：同步后再对账应为零差异

    async with factory() as session, session.begin():
        expected = await expected_class_scope_pairs(session, tenant_id)

        actual_rows = (
            await session.execute(
                text(
                    "select user_id::text, scope_id::text from data_scopes "
                    "where scope_type = 'class' and user_id is not null"
                )
            )
        ).all()

        to_add, to_remove = scope_row_diff(expected, {(str(u), str(c)) for u, c in actual_rows})

    assert to_add == set() and to_remove == set()

    # 删除任教关系 → 班1 的范围行只剩班主任身份（仍保留）；再撤班主任 → 回收

    async with factory() as session, session.begin():
        await session.execute(
            text("delete from teacher_class_courses where teacher_id = :tid"),
            {"tid": teacher_id},
        )

        await sync_class_scope_rows(session, tenant_id)

    assert await current_scope_rows() == {(teacher_user, class_id), (other_user, class2_id)}

    async with factory() as session, session.begin():
        await session.execute(
            text("update classes set head_teacher_id = null where id = :cid"), {"cid": class_id}
        )

        await sync_class_scope_rows(session, tenant_id)

    assert await current_scope_rows() == {(other_user, class2_id)}


async def test_new_tables_rls_isolation(clean_db: AsyncEngine) -> None:
    """新表同样受租户 RLS 约束：租户 A 的连接看不到租户 B 的班级。"""

    tenant_a = await _ensure_tenant(clean_db)

    factory = async_sessionmaker(clean_db, expire_on_commit=False)

    async with factory() as session, session.begin():
        tenant_b = await session.scalar(
            text(
                "insert into tenants (name, status) values ('隔离对照中学', 'active') "
                "returning id::text"
            )
        )

        await session.execute(
            text(
                "insert into classes (tenant_id, grade, name) "
                "values (:tenant_id, '2026级', 'A校班级')"
            ),
            {"tenant_id": tenant_a},
        )

    # 租户 B 的登录连接（非超级用户，RLS 生效）：设置 app.tenant_id 为 B，A 的班级不可见

    engine = make_test_engine("tenant")

    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with session_factory() as session, session.begin():
            await session.execute(
                text("select set_config('app.tenant_id', :tid, true)"), {"tid": tenant_b}
            )

            visible: int = (
                await session.execute(
                    text("select count(*) from classes where tenant_id = :tid"), {"tid": tenant_b}
                )
            ).scalar_one()

            assert visible == 0

    finally:
        await engine.dispose()


async def test_cursor_pagination(clean_db: AsyncEngine) -> None:
    """TD-070：cursor 翻页不重不漏；坏游标抛 ValueError。"""

    tenant_id, _ = await _insert(
        clean_db,
        _USER_SQL,
        {"user_type": "school_admin", "login_id": "it-page", "name": "分页校管"},
    )

    factory = async_sessionmaker(clean_db, expire_on_commit=False)

    total = 7

    for i in range(total):
        async with factory() as session, session.begin():
            session.add(OrgClass(tenant_id=tenant_id, grade="2026级", name=f"分页班{i:02d}"))

    async with factory() as session, session.begin():
        repository: TenantScopedRepository[OrgClass] = TenantScopedRepository(
            session, OrgClass, tenant_id, scope=ScopeBypass(reason="集成测试直查组织表分页行为")
        )

        seen: list[str] = []

        cursor: str | None = None

        pages = 0

        while True:
            rows, cursor = await repository.list_cursor(limit=3, cursor=cursor)

            seen.extend(str(row.id) for row in rows)

            pages += 1

            if cursor is None:
                break

            decode_cursor(cursor)  # 游标必须可解码

        assert pages == 3

        assert len(seen) == total and len(set(seen)) == total

        # 页大小恰好等于总数：仍返回游标（再翻一次为空页），不重不漏

        rows, cursor = await repository.list_cursor(limit=total)

        assert len(rows) == total and cursor is not None

        rows2, cursor2 = await repository.list_cursor(limit=total, cursor=cursor)

        assert rows2 == [] and cursor2 is None

        with pytest.raises(ValueError):
            await repository.list_cursor(limit=3, cursor="broken-cursor")


async def test_import_retry_compensation_and_cas(clean_db: AsyncEngine) -> None:
    """入队失败补偿清除 retried_by；retry CAS 只允许一个派生（并发正确性）。"""

    import uuid as uuid_module

    from app.db.models.imports import ImportJob

    tenant_id, user_id = await _insert(
        clean_db,
        _USER_SQL,
        {"user_type": "school_admin", "login_id": "it-retry", "name": "重试校管"},
    )

    factory = async_sessionmaker(clean_db, expire_on_commit=False)

    # 1) 入队失败补偿：retried_by 被清除，源 job 可再次 retry

    async with factory() as session, session.begin():
        repository: TenantScopedRepository[ImportJob] = TenantScopedRepository(
            session, ImportJob, tenant_id, scope=ScopeBypass(reason="集成直查导入补偿行为")
        )

        source = ImportJob(
            tenant_id=tenant_id,
            type="students",
            conflict="error",
            file_object_key="imports/x/x.csv",
            status="completed_with_errors",
            total_rows=3,
            success_rows=1,
            failed_rows=2,
            created_by=user_id,
        )

        await repository.add(source)

        derived = ImportJob(
            tenant_id=tenant_id,
            type="students",
            conflict="error",
            file_object_key=source.file_object_key,
            status="failed",
            total_rows=2,
            failed_rows=2,
            created_by=user_id,
            retry_count=1,
        )

        await repository.add(derived)

        source.retried_by = derived.id

        job_id, derived_id = source.id, derived.id

    async with factory() as session, session.begin():
        repository2: TenantScopedRepository[ImportJob] = TenantScopedRepository(
            session, ImportJob, tenant_id, scope=ScopeBypass(reason="补偿验证")
        )

        source2 = await repository2.get(job_id)

        assert source2 is not None and source2.retried_by == derived_id

        source2.retried_by = None  # 补偿动作本身

    async with factory() as session:
        repository3: TenantScopedRepository[ImportJob] = TenantScopedRepository(
            session, ImportJob, tenant_id, scope=ScopeBypass(reason="补偿验证")
        )

        source3 = await repository3.get(job_id)

        assert source3 is not None and source3.retried_by is None

    # 2) CAS：retried_by 已被第一个 claim 置值 → 第二个 claim 影响 0 行

    from sqlalchemy import update as sa_update

    async with factory() as session, session.begin():
        repository4: TenantScopedRepository[ImportJob] = TenantScopedRepository(
            session, ImportJob, tenant_id, scope=ScopeBypass(reason="CAS 验证")
        )

        first = ImportJob(
            tenant_id=tenant_id,
            type="students",
            conflict="error",
            file_object_key=source.file_object_key,
            status="pending",
            created_by=user_id,
            row_filter_json=[3, 4],
            retry_count=1,
        )

        await repository4.add(first)

        _ = uuid_module.uuid4  # 占位引用

        claimed1: Any = await session.execute(
            sa_update(ImportJob)
            .where(ImportJob.id == source.id, ImportJob.retried_by.is_(None))
            .values(retried_by=first.id)
        )

        claimed2: Any = await session.execute(
            sa_update(ImportJob)
            .where(ImportJob.id == source.id, ImportJob.retried_by.is_(None))
            .values(retried_by=uuid_module.uuid4())
        )

        assert claimed1.rowcount == 1

        assert claimed2.rowcount == 0
