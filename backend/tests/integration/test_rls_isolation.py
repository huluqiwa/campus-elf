"""RLS 隔离集成测试（真实 PostgreSQL + 非超级用户应用角色）。

覆盖 M1-A 的核心承诺：
- 未设置租户上下文时**读不到任何行、写不进去**（fail-closed），非法/空租户标识同样无行且不报错；
- 带租户上下文时只能看到本租户数据（跨租按条件也查不到）；
- 连接池复用 / 并发下不串租户（`set_config(..., is_local=true)` 随事务失效）；
- 平台路径用**独立连接**（平台角色）读审计，且拿不到业务表权限；
- FORCE RLS 对非超级用户的表属主同样生效。
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models.identity import User
from app.db.models.tenant import Tenant
from tests.support.db import (
    assert_role_is_not_superuser,
    clean_test_data,
    make_session_factory,
    make_test_engine,
    prepare_platform_context,
    prepare_tenant_context,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.integration, pytest.mark.business]

RLS_ERROR = "row-level security"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    """确保测试库结构为最新（迁移失败即失败，不静默跳过）。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    # 护栏：清理会用超级用户 TRUNCATE 关键表，绝不允许指向非测试库
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def tenant_engine(migrated_database: None) -> AsyncIterator[AsyncEngine]:
    """租户侧应用连接（非超级用户，RLS 生效）；用例前用管理连接清空数据。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
    finally:
        await admin_engine.dispose()

    engine = make_test_engine("tenant")
    await assert_role_is_not_superuser(engine)
    yield engine
    await engine.dispose()


@pytest.fixture
async def platform_engine(migrated_database: None) -> AsyncIterator[AsyncEngine]:
    """平台侧应用连接（独立非超级用户角色）。"""

    engine = make_test_engine("platform")
    await assert_role_is_not_superuser(engine)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(tenant_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """租户侧会话工厂。"""

    return make_session_factory(tenant_engine)


@pytest.fixture
def platform_session_factory(platform_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """平台侧会话工厂。"""

    return make_session_factory(platform_engine)


async def _create_tenant(session: AsyncSession, name: str) -> UUID:
    """创建租户（平台级表，无 RLS；租户角色有 tenants 的 SELECT/INSERT）。"""

    tenant = Tenant(name=name, status="active")
    session.add(tenant)
    await session.flush()
    tenant_id: UUID = tenant.id
    return tenant_id


async def _create_user(session: AsyncSession, tenant_id: UUID, login_id: str) -> UUID:
    """在给定租户上下文内创建用户。"""

    user = User(tenant_id=tenant_id, login_id=login_id, user_type="student", password_hash="hash")
    session.add(user)
    await session.flush()
    user_id: UUID = user.id
    return user_id


async def _seed_two_tenants(
    session_factory: async_sessionmaker[AsyncSession],
    platform_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID, UUID, UUID]:
    """建两个租户各一个用户，返回 (租户A, 租户B, 用户A, 用户B)。

    租户（平台表）必须用**平台连接**创建——租户角色对 tenants 只有 SELECT
    （学校开通是平台侧动作，见 TEN-02）；用户用租户连接 + 租户上下文创建。
    """

    async with platform_session_factory() as session, session.begin():
        await prepare_platform_context(session)
        tenant_a = await _create_tenant(session, "示范一中")
        tenant_b = await _create_tenant(session, "示范二中")

    async with session_factory() as session, session.begin():
        await prepare_tenant_context(session, tenant_a)
        user_a = await _create_user(session, tenant_a, "A-2024001")
    async with session_factory() as session, session.begin():
        await prepare_tenant_context(session, tenant_b)
        user_b = await _create_user(session, tenant_b, "B-2024001")
    return tenant_a, tenant_b, user_a, user_b


@pytest.mark.asyncio
async def test_without_tenant_context_reads_nothing(
    session_factory: async_sessionmaker[AsyncSession],
    platform_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """未设置租户上下文：租户表查不到任何行（fail-closed），平台表（tenants）仍可读。"""

    await _seed_two_tenants(session_factory, platform_session_factory)

    async with session_factory() as session, session.begin():
        users = (await session.execute(text("select * from users"))).all()
        tenants = (await session.execute(text("select * from tenants"))).all()

    assert users == []
    # tenants 也启用了 RLS：无租户上下文时同样读不到任何学校（fail-closed）
    assert tenants == []


@pytest.mark.asyncio
async def test_without_tenant_context_insert_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """未设置租户上下文：写入被 WITH CHECK 拒绝（报错信息可读）。"""

    async with session_factory() as session:
        session.add(
            User(tenant_id=uuid4(), login_id="X-1", user_type="student", password_hash="hash")
        )
        with pytest.raises(DBAPIError, match=RLS_ERROR):
            await session.flush()


@pytest.mark.asyncio
async def test_invalid_or_blank_tenant_id_reads_nothing_without_error(
    session_factory: async_sessionmaker[AsyncSession],
    platform_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """非法/空租户标识必须 fail-closed 且**不报错**（不能变成 500）。"""

    await _seed_two_tenants(session_factory, platform_session_factory)

    for raw_value in ("", "not-a-uuid", "   "):
        async with session_factory() as session, session.begin():
            await session.execute(
                text("select set_config('app.tenant_id', :v, true)"), {"v": raw_value}
            )
            rows = (await session.execute(text("select * from users"))).all()
        assert rows == [], f"非法租户标识 {raw_value!r} 竟然读到了数据"


@pytest.mark.asyncio
async def test_tenant_sees_only_own_rows(
    session_factory: async_sessionmaker[AsyncSession],
    platform_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """带租户上下文只能看到本租户数据；按条件查其他租户返回空。"""

    tenant_a, tenant_b, user_a, _ = await _seed_two_tenants(session_factory, platform_session_factory)

    async with session_factory() as session, session.begin():
        await prepare_tenant_context(session, tenant_a)
        users = (await session.execute(text("select id, tenant_id from users"))).all()
        cross = (
            await session.execute(
                text("select id from users where tenant_id = :tid"), {"tid": tenant_b}
            )
        ).all()

    assert [row.tenant_id for row in users] == [tenant_a]
    assert UUID(str(users[0].id)) == user_a
    assert cross == []


@pytest.mark.asyncio
async def test_cross_tenant_write_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    platform_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """在租户 A 上下文里写入租户 B 的数据会被 RLS 拒绝。"""

    tenant_a, tenant_b, _, _ = await _seed_two_tenants(session_factory, platform_session_factory)

    async with session_factory() as session, session.begin():
        await prepare_tenant_context(session, tenant_a)
        session.add(
            User(
                tenant_id=tenant_b,
                login_id="B-9",
                user_type="student",
                password_hash="hash",
            )
        )
        with pytest.raises(DBAPIError, match=RLS_ERROR):
            await session.flush()


@pytest.mark.asyncio
async def test_cross_tenant_parent_reference_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    platform_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """跨租父行引用（把 B 的角色授给 A 的用户）必须被复合外键拒绝。"""

    tenant_a, tenant_b, user_a, _ = await _seed_two_tenants(session_factory, platform_session_factory)

    async with session_factory() as session, session.begin():
        await prepare_tenant_context(session, tenant_b)
        role_b_id: UUID = (
            await session.execute(
                text(
                    "insert into roles (tenant_id, code, name) "
                    "values (:tid, 'r1', '角色B') returning id"
                ),
                {"tid": tenant_b},
            )
        ).scalar_one()

    async with session_factory() as session, session.begin():
        await prepare_tenant_context(session, tenant_a)
        with pytest.raises(DBAPIError) as excinfo:
            await session.execute(
                text("insert into user_roles (tenant_id, user_id, role_id) values (:tid, :uid, :rid)"),
                {"tid": tenant_a, "uid": user_a, "rid": role_b_id},
            )
        # 复合外键或 RLS 任一拦截均可（均为越权写被拒）
        message = str(excinfo.value).lower()
        assert "foreign key" in message or RLS_ERROR in message


@pytest.mark.asyncio
async def test_context_does_not_leak_across_pooled_connections(
    session_factory: async_sessionmaker[AsyncSession],
    platform_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """连接复用：连续多轮切换租户，任何一轮都不得看到其他租户的行。"""

    tenant_a, tenant_b, _, _ = await _seed_two_tenants(session_factory, platform_session_factory)

    observed: list[set[UUID]] = []
    for round_index in range(6):
        tenant_id = tenant_a if round_index % 2 == 0 else tenant_b
        async with session_factory() as session, session.begin():
            await prepare_tenant_context(session, tenant_id)
            rows = (await session.execute(text("select tenant_id from users"))).all()
            observed.append({UUID(str(row.tenant_id)) for row in rows})

    for round_index, seen in enumerate(observed):
        expected = tenant_a if round_index % 2 == 0 else tenant_b
        assert seen == {expected}, f"第 {round_index} 轮出现串租户：{seen}"


@pytest.mark.asyncio
async def test_concurrent_sessions_do_not_mix_tenants(
    session_factory: async_sessionmaker[AsyncSession],
    platform_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """并发会话（模拟多请求）下不得串租户。"""

    tenant_a, tenant_b, _, _ = await _seed_two_tenants(session_factory, platform_session_factory)

    async def read(tenant_id: UUID) -> set[UUID]:
        async with session_factory() as session, session.begin():
            await prepare_tenant_context(session, tenant_id)
            await asyncio.sleep(0.02)  # 制造交错
            rows = (await session.execute(text("select tenant_id from users"))).all()
            return {UUID(str(row.tenant_id)) for row in rows}

    results = await asyncio.gather(*[read(tenant_a if i % 2 == 0 else tenant_b) for i in range(8)])

    for index, seen in enumerate(results):
        expected = tenant_a if index % 2 == 0 else tenant_b
        assert seen == {expected}, f"并发第 {index} 个会话串租户：{seen}"


@pytest.mark.asyncio
async def test_platform_connection_cannot_read_business_tables(
    platform_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """平台连接没有业务表权限（数据库层拒绝，不依赖应用层纪律）。"""

    async with platform_session_factory() as session, session.begin():
        await prepare_platform_context(session)
        with pytest.raises(DBAPIError, match="permission denied"):
            await session.execute(text("select * from users"))


@pytest.mark.asyncio
async def test_tenant_connection_cannot_read_platform_tables(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """租户连接没有平台表权限（平台账号密码哈希不可被租户路径触及）。"""

    async with session_factory() as session, session.begin():
        with pytest.raises(DBAPIError, match="permission denied"):
            await session.execute(text("select * from platform_users"))


@pytest.mark.asyncio
async def test_platform_reads_all_audit_events_while_tenant_reads_only_own(
    session_factory: async_sessionmaker[AsyncSession],
    platform_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """审计读取：平台连接可读全部事件；租户连接只能读本租户事件（含正反断言）。"""

    tenant_a, tenant_b, _, _ = await _seed_two_tenants(session_factory, platform_session_factory)

    insert_sql = text(
        """
        insert into audit_logs (
          event_id, tenant_id, actor_realm, action, resource_type, result, created_at
        ) values (
          gen_random_uuid(), :tid, :realm, :action, 'test', 'success', now()
        )
        """
    )

    async with session_factory() as session, session.begin():
        await prepare_tenant_context(session, tenant_a)
        await session.execute(insert_sql, {"tid": tenant_a, "realm": "tenant", "action": "a.1"})
    async with session_factory() as session, session.begin():
        await prepare_tenant_context(session, tenant_b)
        await session.execute(insert_sql, {"tid": tenant_b, "realm": "tenant", "action": "b.1"})
    async with platform_session_factory() as session, session.begin():
        await prepare_platform_context(session)
        await session.execute(insert_sql, {"tid": None, "realm": "platform", "action": "p.1"})

    async with session_factory() as session, session.begin():
        await prepare_tenant_context(session, tenant_a)
        tenant_a_actions = [
            row.action
            for row in (await session.execute(text("select action from audit_logs"))).all()
        ]

    async with platform_session_factory() as session, session.begin():
        await prepare_platform_context(session)
        platform_actions = sorted(
            row.action
            for row in (await session.execute(text("select action from audit_logs"))).all()
        )

    assert tenant_a_actions == ["a.1"]  # 只见自己的（反向）且能看到自己的（正向）
    assert platform_actions == ["a.1", "b.1", "p.1"]  # 平台可读全部


@pytest.mark.asyncio
async def test_setting_is_transaction_local(
    session_factory: async_sessionmaker[AsyncSession],
    platform_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """上个事务设置的租户上下文在下一个事务中必须已失效。"""

    tenant_a, _, _, _ = await _seed_two_tenants(session_factory, platform_session_factory)

    async with session_factory() as session:
        async with session.begin():
            await prepare_tenant_context(session, tenant_a)
            first = (await session.execute(text("select * from users"))).all()
        async with session.begin():
            second = (await session.execute(text("select * from users"))).all()

    assert len(first) == 1
    assert second == []


@pytest.mark.asyncio
async def test_app_roles_are_not_table_owners_and_cannot_create_tables(
    migrated_database: None,
    tenant_engine: AsyncEngine,
) -> None:
    """应用角色既不是表属主、也不能在 public 建表——无法绕开 RLS 策略。

    说明：若应用角色是表属主，未启用 FORCE RLS 时可绕过策略；本断言确保
    生产形态是"DDL 角色持有表 + 应用角色仅有 DML 权限"。表级 RLS/ FORCE 的
    完整性由 `test_rls_policy_coverage.py` 守住。
    """

    admin_engine = make_test_engine("admin")
    try:
        async with admin_engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        select count(*) as owned
                        from pg_class c
                        join pg_namespace n on n.oid = c.relnamespace
                        join pg_roles r on r.oid = c.relowner
                        where n.nspname = 'public' and c.relkind = 'r'
                          and r.rolname in ('campus_elf_tenant', 'campus_elf_platform',
                                            current_user)
                          and r.rolname <> current_user
                        """
                    )
                )
            ).one()
            tenant_role_owns = (
                await conn.execute(
                    text(
                        """
                        select count(*) from pg_class c
                        join pg_namespace n on n.oid = c.relnamespace
                        join pg_roles r on r.oid = c.relowner
                        where n.nspname = 'public' and c.relkind = 'r'
                          and r.rolname in ('campus_elf_tenant', 'campus_elf_platform')
                        """
                    )
                )
            ).scalar()
        assert row.owned == 0
        assert tenant_role_owns == 0, "应用角色不应成为表属主"
    finally:
        await admin_engine.dispose()

    # 应用角色不能在 public 建表（无法自建表并"忘记"启用策略）
    async with tenant_engine.connect() as conn:
        can_create = await conn.scalar(text("select has_schema_privilege(current_user, 'public', 'CREATE')"))
    assert can_create is False


@pytest.mark.asyncio
async def test_tenant_connection_cannot_escalate_via_platform_guc(
    session_factory: async_sessionmaker[AsyncSession],
    platform_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """租户连接即使自设 `app.platform_context='on'` 也不能读全平台审计/学校，更不能伪造平台事件。

    平台策略已按角色限定（`TO campus_elf_platform`），因此该边界由数据库权限强制，
    而不是靠应用代码"不乱设 GUC"。
    """

    tenant_a, tenant_b, _, _ = await _seed_two_tenants(session_factory, platform_session_factory)

    insert_sql = text(
        """
        insert into audit_logs (
          event_id, tenant_id, actor_realm, action, resource_type, result, created_at
        ) values (gen_random_uuid(), :tid, :realm, :action, 'test', 'success', now())
        """
    )
    async with session_factory() as session, session.begin():
        await prepare_tenant_context(session, tenant_a)
        await session.execute(insert_sql, {"tid": tenant_a, "realm": "tenant", "action": "a.1"})
    async with session_factory() as session, session.begin():
        await prepare_tenant_context(session, tenant_b)
        await session.execute(insert_sql, {"tid": tenant_b, "realm": "tenant", "action": "b.1"})
    async with platform_session_factory() as session, session.begin():
        await prepare_platform_context(session)
        await session.execute(insert_sql, {"tid": None, "realm": "platform", "action": "p.1"})

    # 租户连接伪装平台上下文：审计仍只见自己、学校只见自己那一行
    async with session_factory() as session, session.begin():
        await prepare_tenant_context(session, tenant_a)
        await prepare_platform_context(session)  # 模拟"应用层误设 GUC"
        actions = [
            row.action for row in (await session.execute(text("select action from audit_logs"))).all()
        ]
        tenants_visible = (await session.execute(text("select count(*) from tenants"))).scalar()

        # 伪造平台级事件必须失败（策略按角色限定，租户角色不匹配平台策略）
        with pytest.raises(DBAPIError):
            await session.execute(insert_sql, {"tid": None, "realm": "platform", "action": "forged"})

    assert actions == ["a.1"], f"租户连接读到了非本租户审计：{actions}"
    assert tenants_visible == 1
