"""审计表仅追加测试（AUD-01 的数据库层保证）。

- UPDATE / DELETE / TRUNCATE 必须被数据库拒绝（触发器对表属主同样生效）；
- 平台上下文可写平台事件（tenant_id 为空），租户上下文只能写本租户事件。
"""

import os
from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, InternalError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.db import make_test_engine, upgrade_test_database

pytestmark = [pytest.mark.integration, pytest.mark.business]


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL 未设置")
    upgrade_test_database()
    yield


@pytest.fixture
async def engine(migrated_database: None) -> AsyncIterator[AsyncEngine]:
    """管理连接（超级用户）：用于证明"即使提权，仅追加触发器仍然阻止改删"。"""

    test_engine = make_test_engine("admin")
    async with test_engine.begin() as conn:
        # 仅测试清理：临时关闭触发器（生产禁止任何绕过）
        await conn.execute(text("ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_no_update_delete"))
        await conn.execute(text("ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_no_truncate"))
        await conn.execute(text("TRUNCATE audit_logs"))
        await conn.execute(text("ALTER TABLE audit_logs ENABLE TRIGGER audit_logs_no_update_delete"))
        await conn.execute(text("ALTER TABLE audit_logs ENABLE TRIGGER audit_logs_no_truncate"))
    yield test_engine
    await test_engine.dispose()


async def _insert_platform_event(engine: AsyncEngine, action: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("select set_config('app.platform_context', 'on', true)"))
        await conn.execute(
            text(
                """
                insert into audit_logs (
                  event_id, actor_realm, action, resource_type, resource_id, result, created_at
                ) values (
                  :event_id, 'system', :action, 'test', '1', 'success', now()
                )
                """
            ),
            {"event_id": str(uuid4()), "action": action},
        )


@pytest.mark.asyncio
async def test_update_is_rejected(engine: AsyncEngine) -> None:
    """UPDATE 必须被拒绝（审计不可篡改）。"""

    await _insert_platform_event(engine, "test.insert")

    async with engine.begin() as conn:
        await conn.execute(text("select set_config('app.platform_context', 'on', true)"))
        with pytest.raises((DBAPIError, InternalError), match="append-only"):
            await conn.execute(text("update audit_logs set action = 'tampered'"))


@pytest.mark.asyncio
async def test_delete_is_rejected(engine: AsyncEngine) -> None:
    """DELETE 必须被拒绝。"""

    await _insert_platform_event(engine, "test.insert")

    async with engine.begin() as conn:
        await conn.execute(text("select set_config('app.platform_context', 'on', true)"))
        with pytest.raises((DBAPIError, InternalError), match="append-only"):
            await conn.execute(text("delete from audit_logs"))


@pytest.mark.asyncio
async def test_truncate_is_rejected(engine: AsyncEngine) -> None:
    """TRUNCATE 必须被拒绝（否则一次操作即可抹掉审计）。"""

    await _insert_platform_event(engine, "test.insert")

    async with engine.begin() as conn:
        await conn.execute(text("select set_config('app.platform_context', 'on', true)"))
        with pytest.raises((DBAPIError, InternalError), match="append-only"):
            await conn.execute(text("truncate audit_logs"))


@pytest.mark.asyncio
async def test_insert_allowed_with_platform_context_and_count_grows(engine: AsyncEngine) -> None:
    """插入本身可用，且序号单调增长（哈希链顺序依赖）。"""

    await _insert_platform_event(engine, "test.first")
    await _insert_platform_event(engine, "test.second")

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    select sequence, action from audit_logs
                    order by sequence
                    """
                )
            )
        ).all()

    assert [row.action for row in rows] == ["test.first", "test.second"]
    assert rows[0].sequence < rows[1].sequence


@pytest.mark.asyncio
async def test_app_roles_have_no_update_delete_privilege_on_audit_logs(
    migrated_database: None,
) -> None:
    """权限层：应用角色（租户/平台）对审计表只有 SELECT/INSERT，没有 UPDATE/DELETE/TRUNCATE。"""

    admin_engine = make_test_engine("admin")
    try:
        async with admin_engine.connect() as conn:
            # 覆盖全部 campus_elf% 角色：历史残留角色若仍带 UPDATE/DELETE，这里会失败
            # 只约束**应用角色**（非超级用户）；DDL/迁移角色天然持有全部权限
            role_names = [
                row.rolname
                for row in (
                    await conn.execute(
                        text(
                            "select rolname from pg_roles "
                            "where rolname like 'campus_elf%' and not rolsuper"
                        )
                    )
                ).all()
            ]
            assert role_names, "未发现 campus_elf% 应用角色"
            for role in role_names:
                privileges = {
                    action: await conn.scalar(
                        text("select has_table_privilege(:role, 'audit_logs', :action)"),
                        {"role": role, "action": action},
                    )
                    for action in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                }
                assert privileges == {
                    "SELECT": True,
                    "INSERT": True,
                    "UPDATE": False,
                    "DELETE": False,
                    "TRUNCATE": False,
                }, f"{role} 的审计表权限不符合预期：{privileges}"
    finally:
        await admin_engine.dispose()
