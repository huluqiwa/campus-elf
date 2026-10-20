"""生产会话函数 `db_session()` 的集成覆盖（真实连接池）。

审查发现：此前所有 RLS 用例都走测试侧的 `prepare_tenant_context()` 副本，生产函数
（含 `is_local=true`、平台/租户互斥、UUID 规范化）**零覆盖**。本模块直接测它。
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.db.models.identity import User
from app.db.models.tenant import Tenant
from app.db.session import db_session, dispose_engine
from tests.support.db import (
    clean_test_data,
    database_url,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.integration, pytest.mark.business]


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    """迁移测试库；并对清理用的管理连接做一次性库护栏。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def app_engine_env(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """把应用配置指向测试库的租户/平台角色，并在用例后释放引擎。"""

    admin_engine: AsyncEngine = make_test_engine("admin")
    await clean_test_data(admin_engine)
    await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    get_settings.cache_clear()
    yield
    await dispose_engine()
    get_settings.cache_clear()


async def _create_tenant_via_platform(name: str) -> UUID:
    """用平台连接创建租户（平台侧动作）。"""

    async with db_session(platform=True) as session:
        tenant = Tenant(name=name, status="active")
        session.add(tenant)
        await session.flush()
        return tenant.id


async def _create_user(tenant_id: UUID, login_id: str) -> UUID:
    """用租户连接 + 租户上下文创建用户。"""

    async with db_session(tenant_id=tenant_id) as session:
        user = User(
            tenant_id=tenant_id, login_id=login_id, user_type="student", password_hash="hash"
        )
        session.add(user)
        await session.flush()
        return user.id


@pytest.mark.asyncio
async def test_db_session_enforces_tenant_isolation(app_engine_env: None) -> None:
    """`db_session(tenant_id=...)` 只看到本租户数据；无租户上下文时 fail-closed。"""

    tenant_a = await _create_tenant_via_platform("A 校")
    tenant_b = await _create_tenant_via_platform("B 校")
    await _create_user(tenant_a, "A-1")
    await _create_user(tenant_b, "B-1")

    async with db_session(tenant_id=tenant_a) as session:
        a_rows = (await session.execute(text("select tenant_id from users"))).all()
    async with db_session(tenant_id=tenant_b) as session:
        b_rows = (await session.execute(text("select tenant_id from users"))).all()
    async with db_session() as session:
        anon_rows = (await session.execute(text("select tenant_id from users"))).all()

    assert [UUID(str(row.tenant_id)) for row in a_rows] == [tenant_a]
    assert [UUID(str(row.tenant_id)) for row in b_rows] == [tenant_b]
    assert anon_rows == []


@pytest.mark.asyncio
async def test_db_session_platform_reads_audit_but_not_business_tables(
    app_engine_env: None,
) -> None:
    """平台连接可写读审计事件，但拿不到业务表权限。"""

    insert_sql = text(
        """
        insert into audit_logs (event_id, actor_realm, action, resource_type, result, created_at)
        values (gen_random_uuid(), 'platform', 'test.db_session', 'test', 'success', now())
        """
    )
    async with db_session(platform=True) as session:
        await session.execute(insert_sql)
    async with db_session(platform=True) as session:
        count = (await session.execute(text("select count(*) from audit_logs"))).scalar()

    assert count == 1

    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="permission denied"):
        async with db_session(platform=True) as session:
            await session.execute(text("select * from users"))


@pytest.mark.asyncio
async def test_db_session_rejects_platform_with_tenant(app_engine_env: None) -> None:
    """platform 与 tenant_id 互斥（防止租户请求误用平台连接）。"""

    with pytest.raises(ValueError, match="互斥"):
        async with db_session(platform=True, tenant_id=uuid4()):
            pass  # pragma: no cover - 进入前即抛错


@pytest.mark.asyncio
async def test_db_session_rejects_invalid_tenant_id(app_engine_env: None) -> None:
    """非法租户标识在应用层即报错（不落到数据库变成 500）。"""

    with pytest.raises(ValueError):
        async with db_session(tenant_id="not-a-uuid"):
            pass  # pragma: no cover


@pytest.mark.asyncio
async def test_db_session_rolls_back_on_error(app_engine_env: None) -> None:
    """事务内异常必须整体回滚。"""

    tenant_a = await _create_tenant_via_platform("回滚校")

    with pytest.raises(RuntimeError):
        async with db_session(tenant_id=tenant_a) as session:
            session.add(
                User(
                    tenant_id=tenant_a,
                    login_id="R-1",
                    user_type="student",
                    password_hash="hash",
                )
            )
            await session.flush()
            raise RuntimeError("boom")

    async with db_session(tenant_id=tenant_a) as session:
        rows = (await session.execute(text("select * from users"))).all()
    assert rows == []


@pytest.mark.asyncio
async def test_db_session_context_does_not_leak_across_pool(app_engine_env: None) -> None:
    """连接池复用：连续交替租户，任何一次都不得看到其他租户数据。"""

    tenant_a = await _create_tenant_via_platform("轮换A")
    tenant_b = await _create_tenant_via_platform("轮换B")
    await _create_user(tenant_a, "A-2")
    await _create_user(tenant_b, "B-2")

    seen: list[set[UUID]] = []
    for index in range(6):
        tenant_id = tenant_a if index % 2 == 0 else tenant_b
        async with db_session(tenant_id=tenant_id) as session:
            rows = (await session.execute(text("select tenant_id from users"))).all()
            seen.append({UUID(str(row.tenant_id)) for row in rows})
            await asyncio.sleep(0)

    for index, observed in enumerate(seen):
        expected = tenant_a if index % 2 == 0 else tenant_b
        assert observed == {expected}


@pytest.mark.asyncio
async def test_platform_session_requires_platform_dsn(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未配置 PLATFORM_DATABASE_URL 时平台路径必须显式失败（不得回落到租户连接）。"""

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.delenv("PLATFORM_DATABASE_URL", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="PLATFORM_DATABASE_URL"):
            async with db_session(platform=True):
                pass  # pragma: no cover
    finally:
        await dispose_engine()
        get_settings.cache_clear()
