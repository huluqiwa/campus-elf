"""自扩权守卫的**接线**用例：判据必须读库，不能吃认证依赖那次请求的快照（M2-UI3 TD-191，二轮审查 Minor #5）。

业务场景用 HTTP 跑不出这个差别——`principal.permissions` 与守卫的并集查询是同一条 SQL 形状、
同一个请求里从库里算的，两者只在**并发绑定**时才分叉。这里直接调服务层，用一个"故意落后的
Principal"把两个方向都钉住：

- 快照谎称"我已经有了"（库里没有）→ 仍必须打断：否则伪造一份权限快照就能跳过确认，
  而"跳过确认"正是这条守卫存在的唯一理由；
- 快照漏报（库里经另一个角色已经持有）→ 不得打断：净增为零属日常配置，多问一次就是把正常流挡在门外。

两条合起来才证明用的是库值。把 `set(principal.permissions)` 接回 `_assert_self_grant` 的入参，
这两条必红其一——原有用例全绿，正是因为它们没有制造过快照与库的分叉。
"""

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth.principal import Principal
from app.config import get_settings
from app.db.session import dispose_engine
from app.rbac.admin import RoleAdminError, replace_role_permissions
from tests.support.auth import seed_tenant_user
from tests.support.db import clean_test_data, database_url, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn

pytestmark = pytest.mark.integration

SENSITIVE_CODE = "school.personnel.sensitive.read"
# 目标角色提交后至少保留一项非敏感码：全量替换允许空集，但空集会把"映射是否真的写入"变成看不见的断言
KEEP_CODE = "school.dashboard.view"
PASSWORD = "SelfGr-Wiring-Pass-1"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def stack(migrated_database: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """把 `db_session` 指向测试租户连接（服务层自己开事务，与运行时同一条路径）。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
    finally:
        await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    get_settings.cache_clear()
    yield
    await dispose_engine()
    get_settings.cache_clear()


async def _seed_school(school_code: str, *, second_role_holding_sensitive: bool) -> dict[str, Any]:
    """租户 + 内置角色 + 一个绑在 `school_admin` 上的操作者；可选再经 `teacher_b_end` 持有敏感码。"""

    engine = make_test_engine("admin")
    try:
        tenant_id, user_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"wiring-{school_code}",
            password=PASSWORD,
            user_type="school_admin",
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(text("select seed_builtin_roles(:tid)"), {"tid": str(tenant_id)})
            await session.execute(
                text(
                    "insert into user_roles (tenant_id, user_id, role_id) "
                    "select :tid, :uid, r.id from roles r "
                    "where r.tenant_id = :tid and r.code = 'school_admin'"
                ),
                {"tid": str(tenant_id), "uid": str(user_id)},
            )
            if second_role_holding_sensitive:
                # 敏感码经"另一个角色"流入：库里已持有，但快照可以漏报
                await session.execute(
                    text(
                        "insert into user_roles (tenant_id, user_id, role_id) "
                        "select :tid, :uid, r.id from roles r "
                        "where r.tenant_id = :tid and r.code = 'teacher_b_end'"
                    ),
                    {"tid": str(tenant_id), "uid": str(user_id)},
                )
                await session.execute(
                    text(
                        "insert into role_permissions (id, tenant_id, role_id, permission_id) "
                        "select gen_random_uuid(), :tid, r.id, p.id from roles r, permissions p "
                        "where r.tenant_id = :tid and r.code = 'teacher_b_end' and p.code = :code"
                    ),
                    {"tid": str(tenant_id), "code": SENSITIVE_CODE},
                )
        async with factory() as session:
            role_id = await session.scalar(
                text("select id from roles where tenant_id = :tid and code = 'school_admin'"),
                {"tid": str(tenant_id)},
            )
            version = await session.scalar(
                text("select version from roles where id = :rid"), {"rid": str(role_id)}
            )
        return {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "role_id": UUID(str(role_id)),
            "version": int(version),
        }
    finally:
        await engine.dispose()


def _principal(school: dict[str, Any], permissions: frozenset[str]) -> Principal:
    """构造一个"权限快照=给定值"的主体；`permissions` 故意可以与库里的并集不一致。"""

    return Principal(
        user_id=school["user_id"],
        realm="tenant",
        tenant_id=school["tenant_id"],
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        must_change_password=False,
        roles=frozenset({"school_admin"}),
        permissions=permissions,
    )


async def _db_state(tenant_id: UUID, role_id: UUID) -> tuple[set[str], int]:
    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows: list[Any] = list(
                (
                    await session.execute(
                        text(
                            "select p.code from role_permissions rp "
                            "join permissions p on p.id = rp.permission_id "
                            "where rp.tenant_id = :tid and rp.role_id = :rid"
                        ),
                        {"tid": tenant_id, "rid": role_id},
                    )
                )
                .scalars()
                .all()
            )
            version = int(
                await session.scalar(
                    text("select version from roles where id = :rid"), {"rid": role_id}
                )
            )
    finally:
        await engine.dispose()
    return {str(code) for code in rows}, version


async def _replace(school: dict[str, Any], principal: Principal) -> None:
    await replace_role_permissions(
        principal,
        school["role_id"],
        permission_codes=sorted({KEEP_CODE, SENSITIVE_CODE}),
        version=school["version"],
        reason="接线用例：证明守卫读库而不是读快照",
        request_id=None,
        self_grant_ack=False,
    )


async def test_snapshot_claiming_the_code_is_held_still_interrupts(stack: None) -> None:
    """库里没有该敏感码，快照声称有 → 必须打断。

    这是 fail-open 方向：若守卫吃 `principal.permissions`，一份被伪造/过期的"已持有"就能把确认
    门槛整体绕过，`school.role.self_grant` 审计与 M6 双人确认都失去前提。
    """

    school = await _seed_school("WIRING-A", second_role_holding_sensitive=False)
    with pytest.raises(RoleAdminError) as raised:
        await _replace(school, _principal(school, frozenset({SENSITIVE_CODE})))
    assert raised.value.code == "SELF_GRANT_CONFIRMATION_REQUIRED"
    assert raised.value.http_status == 422

    state, version = await _db_state(school["tenant_id"], school["role_id"])
    assert SENSITIVE_CODE not in state, "打断必须发生在写入之前：映射不能已经进去"
    assert version == school["version"], "版本未前进：守卫必须在 CAS 之前拒绝"


async def test_snapshot_underreporting_a_code_the_db_grants_does_not_interrupt(stack: None) -> None:
    """库里经 `teacher_b_end` 已持有该敏感码，快照漏报 → 不得要求 ack。

    这是"挡正常流"方向：净增为零时把人锁在配置页外，运营会改用"先建临时角色再搬"的野路子，
    守卫反而降低了可审计性。用例同时证明写事务真的落库（版本 +1、码集含敏感码）。
    """

    school = await _seed_school("WIRING-B", second_role_holding_sensitive=True)
    await _replace(school, _principal(school, frozenset()))

    state, version = await _db_state(school["tenant_id"], school["role_id"])
    assert {KEEP_CODE, SENSITIVE_CODE} <= state, state
    assert version == school["version"] + 1, version
