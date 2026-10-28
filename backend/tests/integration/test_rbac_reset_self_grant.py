"""重置出厂接自扩权守卫（M2-UI3 TD-194 收口）。

`reset_role` 是第四条写路径：它按定义表重种权限与范围，不经过 `replace_*` 的入参。权限侧今天
不构成通道（可编辑角色的出厂默认 ∩ 敏感码 = ∅，见单元用例），但**范围侧是可达的**：
`school_admin` 的出厂范围就含 `school`，而同租的另一个管理员可以先把它的范围收窄成 `self`，
再由该角色的持有人点「恢复出厂」把全校可见拿回来——这条路径曾经无 ack、无额外审计。

本用例钉住收口后的行为：

1. 范围净增而未带 `self_grant_ack` → 422 `SELF_GRANT_CONFIRMATION_REQUIRED`，且**不推版本**、
   不重种任何行（拒绝发生在 CAS 之前）；
2. 带 ack → 200，范围回到出厂值，写一条 `school.role.self_grant` 成功审计（`kind="scope"`、
   `confirmed_by="self_grant_ack"`）——与 `school.role.reset` 分开，便于报警与事后撤销；
3. 被打断时写两条拒绝审计：`school.role.reset_denied`（哪条写路径被拒）与一条
   `school.role.self_grant`（哪个敏感维度差点流向本人）。

不测权限维度的打断：可编辑内置角色的出厂默认不含敏感码，造出来只能靠改定义表；
那条不变量由 `tests/test_self_grant_guard_unit.py` 与 `test_rbac_custom_role_constraints.py` 守。
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
from app.db.session import db_session, dispose_engine
from app.rbac import admin
from app.rbac.admin import RoleAdminError
from tests.support.auth import seed_tenant_user
from tests.support.db import clean_test_data, database_url, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn

pytestmark = pytest.mark.integration

PASSWORD = "Reset-Grant-Pass-1"
RESET_REASON = "恢复校方管理员出厂范围（集成用例 TD-194）"


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


def _principal(tenant_id: UUID, user_id: UUID, roles: set[str]) -> Principal:
    return Principal(
        user_id=user_id,
        realm="tenant",
        tenant_id=tenant_id,
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        must_change_password=False,
        roles=frozenset(roles),
        # 判据读库不信快照；这里故意给空集，确保用例不会因为"快照里碰巧有码"而通过
        permissions=frozenset(),
    )


async def _seed(school_code: str) -> dict[str, Any]:
    """超管 + 一名操作者（持 `school.role.manage` 的自定义角色，且也绑在 `school_admin` 上）。

    操作者必须同时是 `school_admin` 的持有人，范围维度的"净增"才会指向他本人；
    只给 `school.role.manage` 的自定义角色（无范围行）时，判据里的 `role_id ∈ principal_role_ids`
    不成立，用例会静默测不到守卫。
    """

    engine = make_test_engine("admin")
    try:
        tenant_id, super_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"reset-super-{school_code}",
            password=PASSWORD,
            user_type="school_admin",
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text("select seed_builtin_roles(:tid)"), {"tid": str(tenant_id)}
            )
            await session.execute(
                text(
                    "insert into public.user_roles (tenant_id, user_id, role_id) "
                    "select :tid, :uid, r.id from public.roles r "
                    "where r.tenant_id = :tid and r.code = 'school_super_admin'"
                ),
                {"tid": str(tenant_id), "uid": str(super_id)},
            )
        _, operator_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"reset-operator-{school_code}",
            password=PASSWORD,
            user_type="school_admin",
            permissions=("school.role.manage",),
        )
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "insert into public.user_roles (tenant_id, user_id, role_id) "
                    "select :tid, :uid, r.id from public.roles r "
                    "where r.tenant_id = :tid and r.code = 'school_admin'"
                ),
                {"tid": str(tenant_id), "uid": str(operator_id)},
            )
    finally:
        await engine.dispose()
    return {
        "tenant_id": tenant_id,
        "super": _principal(tenant_id, super_id, {"school_super_admin"}),
        "operator": _principal(tenant_id, operator_id, set()),
    }


async def _role_id(tenant_id: UUID, code: str) -> UUID:
    async with db_session(tenant_id=tenant_id) as session:
        value = await session.scalar(
            text("select id from public.roles where tenant_id = :tid and code = :code"),
            {"tid": tenant_id, "code": code},
        )
    assert value is not None, code
    return UUID(str(value))


async def _version(tenant_id: UUID, role_id: UUID) -> int:
    async with db_session(tenant_id=tenant_id) as session:
        value = await session.scalar(
            text("select version from public.roles where tenant_id = :tid and id = :rid"),
            {"tid": tenant_id, "rid": role_id},
        )
    assert value is not None
    return int(value)


async def _scopes(tenant_id: UUID, role_id: UUID) -> set[str]:
    async with db_session(tenant_id=tenant_id) as session:
        rows: list[Any] = list(
            (
                await session.execute(
                    text(
                        "select scope_type from public.data_scopes "
                        "where tenant_id = :tid and role_id = :rid"
                    ),
                    {"tid": tenant_id, "rid": role_id},
                )
            )
            .scalars()
            .all()
        )
    return {str(item) for item in rows}


async def _audits(action: str) -> list[dict[str, Any]]:
    async with db_session(platform=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select result, resource_id, metadata_json from public.audit_logs "
                        "where action = :action order by sequence"
                    ),
                    {"action": action},
                )
            )
            .mappings()
            .all()
        )
    return [
        {
            "result": str(row["result"]),
            "resource_id": None if row["resource_id"] is None else str(row["resource_id"]),
            "metadata": dict(row["metadata_json"] or {}),
        }
        for row in rows
    ]


async def test_reset_scope_net_increase_requires_ack_and_is_then_audited(stack: None) -> None:
    del stack
    seeded = await _seed(f"SCH-RSG{uuid4().hex[:6].upper()}")
    tenant_id = seeded["tenant_id"]
    role_id = await _role_id(tenant_id, "school_admin")
    assert "school" in await _scopes(tenant_id, role_id), "出厂范围应含 school（矩阵 §2）"
    # 内置角色经 `seed_builtin_roles` 落地时已 +1（标记 defaults_applied），版本不是 0
    seeded_version = await _version(tenant_id, role_id)

    # 先把范围收窄成 self（缩小不触发守卫），制造出"重置 = 范围净增"的场景
    narrowed = await admin.replace_role_scopes(
        seeded["super"],
        role_id,
        scope_types={"self"},
        version=seeded_version,
        reason=None,
        request_id=None,
    )
    narrowed_version = narrowed.version
    assert narrowed_version == seeded_version + 1
    assert await _scopes(tenant_id, role_id) == {"self"}

    # ① 无 ack 的重置：422，且拒绝发生在版本 CAS 之前——版本、范围行都不动
    with pytest.raises(RoleAdminError) as refused:
        await admin.reset_role(
            seeded["operator"],
            role_id,
            confirm_role_code="school_admin",
            reason=RESET_REASON,
            version=narrowed_version,
            request_id=None,
        )
    assert (refused.value.code, refused.value.http_status) == (
        "SELF_GRANT_CONFIRMATION_REQUIRED",
        422,
    )
    assert await _version(tenant_id, role_id) == narrowed_version, "被拒的重置不得推高版本"
    assert await _scopes(tenant_id, role_id) == {"self"}, "被拒的重置不得重种范围"

    denied = await _audits("school.role.self_grant")
    assert [(entry["result"], entry["metadata"].get("reason")) for entry in denied] == [
        ("denied", "SELF_GRANT_CONFIRMATION_REQUIRED")
    ], denied
    assert denied[0]["metadata"]["kind"] == "scope"
    assert denied[0]["metadata"]["newly_reaching_self"] == ["school"]
    reset_denied = await _audits("school.role.reset_denied")
    assert [entry["metadata"].get("reason") for entry in reset_denied] == [
        "SELF_GRANT_CONFIRMATION_REQUIRED"
    ]
    assert await _audits("school.role.reset") == []

    # ② 带 ack：200，范围回到出厂值；成功侧留一条独立的自扩权审计
    applied = await admin.reset_role(
        seeded["operator"],
        role_id,
        confirm_role_code="school_admin",
        reason=RESET_REASON,
        version=narrowed_version,
        request_id=None,
        self_grant_ack=True,
    )
    assert applied.version == narrowed_version + 1, "成功重置才推版本"
    assert await _scopes(tenant_id, role_id) == {"school"}
    assert await _version(tenant_id, role_id) == narrowed_version + 1

    self_grant = await _audits("school.role.self_grant")
    assert [(entry["result"], entry["metadata"].get("reason")) for entry in self_grant] == [
        ("denied", "SELF_GRANT_CONFIRMATION_REQUIRED"),
        ("success", RESET_REASON),
    ], self_grant
    assert self_grant[1]["metadata"]["kind"] == "scope"
    assert self_grant[1]["metadata"]["confirmed_by"] == "self_grant_ack"
    assert self_grant[1]["metadata"]["newly_reaching_self"] == ["school"]

    # 重置本身仍是一条普通流水审计：流程审计与安全信号分开记，报警只订后者
    reset = await _audits("school.role.reset")
    assert [(entry["result"], entry["resource_id"]) for entry in reset] == [
        ("success", str(role_id))
    ]
    assert reset[0]["metadata"]["scopes_added"] == ["school"]
    assert reset[0]["metadata"]["scopes_removed"] == ["self"]


async def test_reset_without_scope_expansion_needs_no_ack(stack: None) -> None:
    """出厂范围与当前一致（净增为零）时一律放行：守卫不能把日常"恢复默认"也挡在门外。"""

    del stack
    seeded = await _seed(f"SCH-RSG{uuid4().hex[:6].upper()}")
    tenant_id = seeded["tenant_id"]
    role_id = await _role_id(tenant_id, "school_admin")
    seeded_version = await _version(tenant_id, role_id)

    applied = await admin.reset_role(
        seeded["operator"],
        role_id,
        confirm_role_code="school_admin",
        reason=RESET_REASON,
        version=seeded_version,
        request_id=None,
    )
    assert applied.version == seeded_version + 1
    assert await _scopes(tenant_id, role_id) == {"school"}
    assert await _audits("school.role.self_grant") == []
    assert [
        entry["result"] for entry in await _audits("school.role.reset")
    ] == ["success"]
