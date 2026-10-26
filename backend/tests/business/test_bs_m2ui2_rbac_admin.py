"""业务场景 BS-M2UI2-A：角色权限配置与重置出厂（TD-137/TD-143，HTTP 真实链路）。

Given 校方超管与一名绑定 `school_admin` 内置角色的管理员，
When 超管改角色权限、并发覆盖/越权/锁定尝试、重置出厂，
Then 权限下一请求热生效且不踢会话、版本锁拒绝静默覆盖、绑定用户数与审计 diff 可核对、
    重置同时恢复权限与数据范围且预览不写库。

TD-150 的「清空不被同步补回」也在本文件用真实同步函数复验一次（接口清空 → 同步 → 仍为空）。
"""

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth import revocation
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.main import create_app
from app.rbac import admin as rbac_admin
from app.rbac import sync_rbac_definitions
from app.rbac.catalog import PERMISSION_CATALOG, TENANT_MODULES, PermissionDef, permission_def
from tests.support.auth import login_tenant, seed_tenant_user
from tests.support.db import clean_test_data, database_url, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.business, pytest.mark.integration]

SUPER_LOGIN = "rbac-super"
SUPER_PASSWORD = "Rbac-Super-Pass-1"
ADMIN_LOGIN = "rbac-admin"
ADMIN_PASSWORD = "Rbac-Admin-Pass-1"

SCHOOL_ADMIN_DEFAULTS = {
    "school.dashboard.view",
    "school.personnel.view",
    "school.kb.view",
    "school.message.view",
    "school.message.send",
    "school.message.read",
}
TENANT_CODES = {entry.code for entry in PERMISSION_CATALOG if entry.module in TENANT_MODULES}


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def client(migrated_database: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
    finally:
        await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    get_settings.cache_clear()
    await revocation.reset_client()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()


async def _seed_school(school_code: str) -> dict[str, Any]:
    """建租户 + 内置角色；超管绑 `school_super_admin`，管理员绑 `school_admin`。

    `seed_tenant_user` 直建的租户没有内置角色，必须先 `seed_builtin_roles(tenant_id)`
    再绑定——这正是开通函数的真实链路（版本号由函数推进，不能假设从 0 开始）。
    """

    engine = make_test_engine("admin")
    try:
        tenant_id, super_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=SUPER_LOGIN,
            password=SUPER_PASSWORD,
            user_type="school_admin",
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text("select seed_builtin_roles(:tid)"), {"tid": str(tenant_id)}
            )
        _, admin_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=ADMIN_LOGIN,
            password=ADMIN_PASSWORD,
            user_type="school_admin",
        )
        async with factory() as session, session.begin():
            for user_id, role_code in (
                (super_id, "school_super_admin"),
                (admin_id, "school_admin"),
            ):
                await session.execute(
                    text(
                        "insert into user_roles (tenant_id, user_id, role_id) "
                        "select :tid, :uid, r.id from roles r "
                        "where r.tenant_id = :tid and r.code = :code"
                    ),
                    {"tid": str(tenant_id), "uid": str(user_id), "code": role_code},
                )
        return {"tenant_id": tenant_id, "super_id": super_id, "admin_id": admin_id}
    finally:
        await engine.dispose()


async def _seed_bare_school(school_code: str) -> dict[str, Any]:
    """只建租户与内置角色（跨租断言用，不建可登录账号）。"""

    engine = make_test_engine("admin")
    try:
        tenant_id, user_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id="rbac-foreign",
            password="Rbac-Foreign-Pass-1",
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text("select seed_builtin_roles(:tid)"), {"tid": str(tenant_id)}
            )
        return {"tenant_id": tenant_id, "user_id": user_id}
    finally:
        await engine.dispose()


async def _role_id(tenant_id: UUID, code: str) -> UUID:
    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            value = await session.scalar(
                text("select id from roles where tenant_id = :tid and code = :code"),
                {"tid": str(tenant_id), "code": code},
            )
    finally:
        await engine.dispose()
    assert value is not None
    return UUID(str(value))


async def _db_permission_codes(tenant_id: UUID, role_id: UUID) -> set[str]:
    async with db_session(tenant_id=tenant_id) as session:
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
    return {str(code) for code in rows}


async def _db_role_scopes(tenant_id: UUID, role_id: UUID) -> set[str]:
    async with db_session(tenant_id=tenant_id) as session:
        rows: list[Any] = list(
            (
                await session.execute(
                    text(
                        "select scope_type from data_scopes "
                        "where tenant_id = :tid and role_id = :rid"
                    ),
                    {"tid": tenant_id, "rid": role_id},
                )
            )
            .scalars()
            .all()
        )
    return {str(scope) for scope in rows}


async def _role_row(tenant_id: UUID, role_id: UUID) -> dict[str, Any]:
    async with db_session(tenant_id=tenant_id) as session:
        row = (
            (
                await session.execute(
                    text(
                        "select version, defaults_applied from roles "
                        "where tenant_id = :tid and id = :rid"
                    ),
                    {"tid": tenant_id, "rid": role_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _audit_metadata(action: str) -> list[dict[str, Any]]:
    async with db_session(platform=True) as session:
        rows: list[Any] = list(
            (
                await session.execute(
                    text(
                        "select metadata_json from audit_logs where action = :action "
                        "order by sequence"
                    ),
                    {"action": action},
                )
            )
            .scalars()
            .all()
        )
    return [dict(row or {}) for row in rows]


async def _audit_entries(action: str) -> list[dict[str, Any]]:
    """成对取回 `result / resource_id / metadata_json`：拒绝留痕要同时核对状态码语义与对象。"""

    async with db_session(platform=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select result, resource_id, metadata_json from audit_logs "
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


def _role_payload(roles: list[dict[str, Any]], code: str) -> dict[str, Any]:
    return next(role for role in roles if role["code"] == code)


async def test_roles_and_catalog_are_tenant_scoped_and_permissioned(client: AsyncClient) -> None:
    """Given 超管与 school_admin 管理员；When 读角色/权限目录；Then 只有租户域目录，改配置需 role.manage。"""

    school_code = f"SCH-RBAC{uuid4().hex[:6].upper()}"
    await _seed_school(school_code)
    super_headers = await login_tenant(
        client, school_code=school_code, login_id=SUPER_LOGIN, password=SUPER_PASSWORD
    )

    roles = await client.get("/api/v1/school/roles", headers=super_headers)
    assert roles.status_code == 200, roles.text
    by_code = {role["code"]: role for role in roles.json()}
    assert set(by_code) == {
        "school_super_admin",
        "school_admin",
        "teacher_b_end",
        "student",
        "teacher",
    }
    assert by_code["school_super_admin"]["editable"] is False
    assert "防止自锁" in by_code["school_super_admin"]["lock_reason"]
    assert by_code["school_admin"]["editable"] is True
    assert by_code["school_admin"]["lock_reason"] is None
    assert by_code["teacher_b_end"]["editable"] is True
    for locked in ("student", "teacher"):
        assert by_code[locked]["editable"] is False
        assert by_code[locked]["lock_reason"]
    # 超管出厂即全量（school+campus），且列表回的是权限码不是平台码
    assert set(by_code["school_super_admin"]["permission_codes"]) == TENANT_CODES
    assert set(by_code["school_admin"]["permission_codes"]) == SCHOOL_ADMIN_DEFAULTS
    assert by_code["school_admin"]["user_count"] == 1
    assert by_code["school_admin"]["defaults_applied"] is True

    catalog = await client.get("/api/v1/school/permissions", headers=super_headers)
    assert catalog.status_code == 200, catalog.text
    items = catalog.json()
    assert {item["code"] for item in items} == TENANT_CODES
    assert [item["code"] for item in items] == sorted(
        (item["code"] for item in items), key=lambda code: (code.split(".", 1)[0], code)
    )
    assert "school.personnel.mfa.reset" in {item["code"] for item in items}
    assert "school.role.manage" in {item["code"] for item in items}
    mfa_entry = next(item for item in items if item["code"] == "school.personnel.mfa.reset")
    assert mfa_entry["module"] == "school" and mfa_entry["action"] == "reset"
    assert mfa_entry["description"]

    # school_admin 默认不含 school.role.manage：读列表即 403（前端隐藏不算边界）
    admin_headers = await login_tenant(
        client, school_code=school_code, login_id=ADMIN_LOGIN, password=ADMIN_PASSWORD
    )
    denied = await client.get("/api/v1/school/roles", headers=admin_headers)
    assert denied.status_code == 403, denied.text


async def test_permission_replace_is_hot_versioned_and_audited(client: AsyncClient) -> None:
    """Given school_admin 角色与绑定用户；When 全量替换权限；Then 下一请求生效、会话不断、并发/非法码被拒。"""

    school_code = f"SCH-RBAC{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    super_headers = await login_tenant(
        client, school_code=school_code, login_id=SUPER_LOGIN, password=SUPER_PASSWORD
    )
    admin_headers = await login_tenant(
        client, school_code=school_code, login_id=ADMIN_LOGIN, password=ADMIN_PASSWORD
    )
    role_id = await _role_id(seeded["tenant_id"], "school_admin")

    before = _role_payload(
        (await client.get("/api/v1/school/roles", headers=super_headers)).json(), "school_admin"
    )
    assert set(before["permission_codes"]) == SCHOOL_ADMIN_DEFAULTS
    version = before["version"]

    # 管理员当前可读人员（默认 school.personnel.view）
    assert (
        await client.get("/api/v1/school/personnel?limit=5", headers=admin_headers)
    ).status_code == 200

    # When：撤掉 personnel.view、加上 role.manage（保留会话可继续访问的入口）
    new_codes = sorted(
        (SCHOOL_ADMIN_DEFAULTS - {"school.personnel.view"}) | {"school.role.manage"}
    )
    replaced = await client.put(
        f"/api/v1/school/roles/{role_id}/permissions",
        json={
            "permission_codes": new_codes,
            "version": version,
            "reason": "调整校方授权管理员职责（场景 BS-M2UI2-A）",
        },
        headers=super_headers,
    )
    assert replaced.status_code == 200, replaced.text
    body = replaced.json()
    assert body["version"] == version + 1
    assert body["permission_codes"] == new_codes
    assert set(await _db_permission_codes(seeded["tenant_id"], role_id)) == set(new_codes)

    # Then：同一条 access token 下一请求即生效，会话本身未被撤销
    assert (
        await client.get("/api/v1/school/personnel?limit=5", headers=admin_headers)
    ).status_code == 403
    assert (
        await client.get("/api/v1/school/permissions", headers=admin_headers)
    ).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=admin_headers)).status_code == 200

    audit_rows = await _audit_metadata("school.role.permissions.updated")
    assert len(audit_rows) == 1
    metadata = audit_rows[0]
    assert metadata["role_code"] == "school_admin"
    assert metadata["added"] == ["school.role.manage"]
    assert metadata["removed"] == ["school.personnel.view"]
    assert "BS-M2UI2-A" in metadata["reason"]

    # 并发覆盖：旧版本必须 409 并带最新快照，库不被改动
    stale = await client.put(
        f"/api/v1/school/roles/{role_id}/permissions",
        json={"permission_codes": sorted(SCHOOL_ADMIN_DEFAULTS), "version": version},
        headers=super_headers,
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "ROLE_VERSION_CONFLICT"
    assert stale.json()["detail"]["latest"]["version"] == version + 1
    assert stale.json()["detail"]["latest"]["permission_codes"] == new_codes
    assert set(await _db_permission_codes(seeded["tenant_id"], role_id)) == set(new_codes)

    # 平台域码与幽灵码：422，且不能借租户角色拿到平台能力
    current_version = version + 1
    leaked = await client.put(
        f"/api/v1/school/roles/{role_id}/permissions",
        json={"permission_codes": ["platform.audit.view"], "version": current_version},
        headers=super_headers,
    )
    assert leaked.status_code == 422, leaked.text
    assert leaked.json()["detail"]["code"] == "PLATFORM_PERMISSION_FORBIDDEN"
    ghost = await client.put(
        f"/api/v1/school/roles/{role_id}/permissions",
        json={"permission_codes": ["school.not.exists"], "version": current_version},
        headers=super_headers,
    )
    assert ghost.status_code == 422, ghost.text
    assert ghost.json()["detail"]["code"] == "UNKNOWN_PERMISSION_CODE"
    assert set(await _db_permission_codes(seeded["tenant_id"], role_id)) == set(new_codes)


async def test_permission_replace_rejects_catalog_drift_before_writing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given 代码目录已新增码但 `sync_rbac_definitions()` 还没跑（库里 `permissions` 查不到）；
    Then 409 提示先执行同步，且不删旧映射、不前进版本。

    挂载走的是 `join permissions on code = ...`：缺码时**静默少挂一条**，提交 N 项只生效
    N-1 项，接口还回 200——管理员以为撤掉了/加上了，实际没有。漂移必须是错误而不是少一条，
    所以这里不查"提交码是否在 Python 目录里"（`_validate_codes` 已查），而查"是否在库里"。
    """

    school_code = f"SCH-RBAC{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    super_headers = await login_tenant(
        client, school_code=school_code, login_id=SUPER_LOGIN, password=SUPER_PASSWORD
    )
    role_id = await _role_id(seeded["tenant_id"], "school_admin")
    before = _role_payload(
        (await client.get("/api/v1/school/roles", headers=super_headers)).json(), "school_admin"
    )

    # 只在测试进程里把幽灵码"加进代码目录"：库里没有对应 permissions 行，等价于
    # 代码已发版、迁移/同步还没跑的那个窗口。不动全局 `permissions` 表——它的两条
    # 外键都是 CASCADE，删一行会连带删掉出厂定义与其它租户的映射，污染同库后续用例。
    ghost = "school.not.yet.synced"
    # 真实清单仍走 catalog 的原函数（`app.rbac.admin` 里的同名符号是 import 进来的，
    # 直接 `rbac_admin.permission_def` 会被 mypy 的 no-implicit-reexport 拒掉）
    real_permission_def = permission_def

    def catalog_with_ghost(code: str) -> Any:
        if code == ghost:
            return PermissionDef(
                code=ghost, module="school", action="view", description="待同步的权限码"
            )
        return real_permission_def(code)

    monkeypatch.setattr(rbac_admin, "permission_def", catalog_with_ghost)

    drifted = await client.put(
        f"/api/v1/school/roles/{role_id}/permissions",
        json={
            "permission_codes": sorted(SCHOOL_ADMIN_DEFAULTS | {ghost}),
            "version": int(before["version"]),
            "reason": "模拟代码目录领先数据库（场景 BS-M2UI2-F）",
        },
        headers=super_headers,
    )
    assert drifted.status_code == 409, drifted.text
    assert drifted.json()["detail"]["code"] == "PERMISSION_CATALOG_OUT_OF_SYNC"
    # 探针在任何写之前：旧映射一条没删、版本没前进
    assert int((await _role_row(seeded["tenant_id"], role_id))["version"]) == int(before["version"])
    assert set(await _db_permission_codes(seeded["tenant_id"], role_id)) == SCHOOL_ADMIN_DEFAULTS


async def test_locked_foreign_and_custom_roles_are_rejected(client: AsyncClient) -> None:
    """Given 锁定角色、自定义角色与另一租户的角色；When 试图改/重置；Then 409 或 404，不泄漏对方数据。"""

    school_code = f"SCH-RBAC{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    super_headers = await login_tenant(
        client, school_code=school_code, login_id=SUPER_LOGIN, password=SUPER_PASSWORD
    )
    roles = (await client.get("/api/v1/school/roles", headers=super_headers)).json()

    # 锁定角色：改权限与重置都 409，且给出人读原因
    super_admin = _role_payload(roles, "school_super_admin")
    locked_put = await client.put(
        f"/api/v1/school/roles/{super_admin['id']}/permissions",
        json={"permission_codes": [], "version": super_admin["version"]},
        headers=super_headers,
    )
    assert locked_put.status_code == 409, locked_put.text
    assert locked_put.json()["detail"]["code"] == "ROLE_IMMUTABLE"
    locked_preview = await client.post(
        f"/api/v1/school/roles/{super_admin['id']}/reset/preview", headers=super_headers
    )
    assert locked_preview.status_code == 409
    assert locked_preview.json()["detail"]["code"] == "ROLE_IMMUTABLE"
    # 绕过预览直接执行重置：同样 409（前端的按钮禁用只是 UX，服务端才是门禁）
    locked_reset = await client.post(
        f"/api/v1/school/roles/{super_admin['id']}/reset",
        json={
            "confirm_role_code": super_admin["code"],
            "reason": "试图把超管恢复出厂以覆盖既有配置",
            "version": super_admin["version"],
        },
        headers=super_headers,
    )
    assert locked_reset.status_code == 409, locked_reset.text
    assert locked_reset.json()["detail"]["code"] == "ROLE_IMMUTABLE"
    student = _role_payload(roles, "student")
    student_put = await client.put(
        f"/api/v1/school/roles/{student['id']}/permissions",
        json={"permission_codes": ["campus.chat.use"], "version": student["version"]},
        headers=super_headers,
    )
    assert student_put.status_code == 409

    # 自定义角色不在本期可编辑范围
    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            custom_id = await session.scalar(
                text(
                    "insert into roles (tenant_id, code, name, is_system) "
                    "values (:tid, :code, '运营自建', false) returning id"
                ),
                {"tid": str(seeded["tenant_id"]), "code": f"custom_{uuid4().hex[:6]}"},
            )
    finally:
        await engine.dispose()
    custom_preview = await client.post(
        f"/api/v1/school/roles/{custom_id}/reset/preview", headers=super_headers
    )
    assert custom_preview.status_code == 409
    assert custom_preview.json()["detail"]["code"] == "ROLE_IMMUTABLE"
    assert "M2-UI3" in custom_preview.json()["detail"]["message"]
    custom_reset = await client.post(
        f"/api/v1/school/roles/{custom_id}/reset",
        json={
            "confirm_role_code": "unused",
            "reason": "试图重置尚未开放的自定义角色",
            "version": 0,
        },
        headers=super_headers,
    )
    assert custom_reset.status_code == 409, custom_reset.text
    assert custom_reset.json()["detail"]["code"] == "ROLE_IMMUTABLE"

    # 不存在与跨租同形 404：不区分「没有」与「不是你的」
    missing_id = uuid4()
    missing_put = await client.put(
        f"/api/v1/school/roles/{missing_id}/permissions",
        json={"permission_codes": [], "version": 0},
        headers=super_headers,
    )
    assert missing_put.status_code == 404

    foreign_code = f"SCH-RBAC{uuid4().hex[:6].upper()}"
    foreign = await _seed_bare_school(foreign_code)
    foreign_role_id = await _role_id(foreign["tenant_id"], "school_admin")
    foreign_put = await client.put(
        f"/api/v1/school/roles/{foreign_role_id}/permissions",
        json={"permission_codes": [], "version": 0},
        headers=super_headers,
    )
    assert foreign_put.status_code == 404, foreign_put.text
    listed_ids = {UUID(role["id"]) for role in roles}
    assert foreign_role_id not in listed_ids
    # 对方角色未被误改
    assert (
        await _db_permission_codes(foreign["tenant_id"], foreign_role_id)
        == SCHOOL_ADMIN_DEFAULTS
    )
    # 试图重置别人家的角色：同样 404（预览与 PUT 都不暴露存在性）
    foreign_reset = await client.post(
        f"/api/v1/school/roles/{foreign_role_id}/reset",
        json={
            "confirm_role_code": "school_admin",
            "reason": "试图重置其他租户的同名角色",
            "version": 0,
        },
        headers=super_headers,
    )
    assert foreign_reset.status_code == 404, foreign_reset.text

    # 安全拒绝必须留痕（外层事务已回滚，同事务写的审计会一起消失——这里验独立事务生效）。
    # 预览是只读接口、约定不审计；留痕来自「改权限」与「执行重置」两类写操作。
    # 版本冲突/确认名不符属"用错了"而不是"越权"，不落安全审计。
    denied_permissions = await _audit_entries("school.role.permissions.denied")
    assert [(e["result"], e["metadata"].get("reason")) for e in denied_permissions] == [
        ("denied", "ROLE_IMMUTABLE"),
        ("denied", "ROLE_IMMUTABLE"),
        ("denied", "ROLE_NOT_FOUND"),
        ("denied", "ROLE_NOT_FOUND"),
    ], denied_permissions
    assert {e["resource_id"] for e in denied_permissions} == {
        str(super_admin["id"]),
        str(student["id"]),
        str(missing_id),
        str(foreign_role_id),
    }
    denied_resets = await _audit_entries("school.role.reset_denied")
    assert [(e["result"], e["metadata"].get("reason")) for e in denied_resets] == [
        ("denied", "ROLE_IMMUTABLE"),
        ("denied", "ROLE_IMMUTABLE"),
        ("denied", "ROLE_NOT_FOUND"),
    ], denied_resets
    assert {e["resource_id"] for e in denied_resets} == {
        str(super_admin["id"]),
        str(custom_id),
        str(foreign_role_id),
    }


async def test_role_manage_code_cannot_be_delegated(client: AsyncClient) -> None:
    """Given 超管把 `school.role.manage` 授给 school_admin；When 该管理员试图转授这个码；
    Then 422 且不前进版本、库未改动、拒绝留审计；不含该码的常规编辑仍然放行，超管仍可授/撤；
    末尾钉住禁令的真实边界（放弃该码换别的敏感码不拦，TD-191）。

    转授即等于无条件把本校全部权限交出去（持有者能给自己所在角色补任意 school/campus 码），
    所以这个码只能由校方超管本人授出（用户 2026-10-26 决策）。
    """

    school_code = f"SCH-RBAC{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    super_headers = await login_tenant(
        client, school_code=school_code, login_id=SUPER_LOGIN, password=SUPER_PASSWORD
    )
    admin_headers = await login_tenant(
        client, school_code=school_code, login_id=ADMIN_LOGIN, password=ADMIN_PASSWORD
    )
    admin_role_id = await _role_id(seeded["tenant_id"], "school_admin")
    teacher_role_id = await _role_id(seeded["tenant_id"], "teacher_b_end")

    # 超管授予 role.manage（允许：主体持有 school_super_admin）
    granted = await client.put(
        f"/api/v1/school/roles/{admin_role_id}/permissions",
        json={
            "permission_codes": sorted(SCHOOL_ADMIN_DEFAULTS | {"school.role.manage"}),
            "version": int(_role_payload(
                (await client.get("/api/v1/school/roles", headers=super_headers)).json(),
                "school_admin",
            )["version"]),
            "reason": "超管委托管理员代管角色权限（场景 BS-M2UI2-E）",
        },
        headers=super_headers,
    )
    assert granted.status_code == 200, granted.text
    delegated_version = int(granted.json()["version"])
    assert "school.role.manage" in granted.json()["permission_codes"]

    # 管理员反向把 role.manage 连同敏感查看码写进自己所在角色 → 422，不生效
    escalated = await client.put(
        f"/api/v1/school/roles/{admin_role_id}/permissions",
        json={
            "permission_codes": sorted(
                SCHOOL_ADMIN_DEFAULTS
                | {"school.role.manage", "school.personnel.sensitive.read"}
            ),
            "version": delegated_version,
            "reason": "试图转授角色管理权（场景 BS-M2UI2-E）",
        },
        headers=admin_headers,
    )
    assert escalated.status_code == 422, escalated.text
    assert escalated.json()["detail"]["code"] == "ROLE_CODE_NOT_DELEGABLE"
    assert "school.role.manage" in escalated.json()["detail"]["message"]
    # 版本未前进、映射未变（拒绝发生在任何写之前）
    assert int((await _role_row(seeded["tenant_id"], admin_role_id))["version"]) == (
        delegated_version
    )
    assert set(await _db_permission_codes(seeded["tenant_id"], admin_role_id)) == set(
        SCHOOL_ADMIN_DEFAULTS | {"school.role.manage"}
    )
    denied = await _audit_entries("school.role.permissions.denied")
    assert [
        (e["result"], e["metadata"].get("reason"), e["resource_id"]) for e in denied
    ] == [("denied", "ROLE_CODE_NOT_DELEGABLE", str(admin_role_id))], denied

    # 只挡转授，不挡常规配置：管理员编辑其它角色（不含该码）仍成功
    routine = await client.put(
        f"/api/v1/school/roles/{teacher_role_id}/permissions",
        json={
            "permission_codes": ["campus.chat.use"],
            "version": int(_role_payload(
                (await client.get("/api/v1/school/roles", headers=admin_headers)).json(),
                "teacher_b_end",
            )["version"]),
            "reason": "收敛教师 B 端权限（场景 BS-M2UI2-E）",
        },
        headers=admin_headers,
    )
    assert routine.status_code == 200, routine.text
    assert set(routine.json()["permission_codes"]) == {"campus.chat.use"}

    # 超管仍可撤销该码（收回委托）
    revoked = await client.put(
        f"/api/v1/school/roles/{admin_role_id}/permissions",
        json={
            "permission_codes": sorted(SCHOOL_ADMIN_DEFAULTS),
            "version": delegated_version,
            "reason": "收回角色管理委托（场景 BS-M2UI2-E）",
        },
        headers=super_headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert "school.role.manage" not in revoked.json()["permission_codes"]

    # 禁令的**真实边界**（TD-191 登记的残余风险，这里钉成用例而不是只写在文档里）：
    # 「只有超管能授出该码」并不等于「拿不到别的敏感码」。先由超管把该码授回
    # （撤销后管理员连 PUT 的资格都没有，上面的 403 正是这个意思），再由管理员自己
    # 提交一份**不含** role.manage 的权限集——把 role.manage 换成敏感查看权，接口放行。
    regrant = await client.put(
        f"/api/v1/school/roles/{admin_role_id}/permissions",
        json={
            "permission_codes": sorted(SCHOOL_ADMIN_DEFAULTS | {"school.role.manage"}),
            "version": int(revoked.json()["version"]),
            "reason": "重新委托角色管理权（TD-191 边界用例前置）",
        },
        headers=super_headers,
    )
    assert regrant.status_code == 200, regrant.text
    traded = await client.put(
        f"/api/v1/school/roles/{admin_role_id}/permissions",
        json={
            "permission_codes": sorted(
                SCHOOL_ADMIN_DEFAULTS | {"school.personnel.sensitive.read"}
            ),
            "version": int(regrant.json()["version"]),
            "reason": "管理员自助改敏感查看权（TD-191 边界用例）",
        },
        headers=admin_headers,
    )
    assert traded.status_code == 200, traded.text
    assert "school.personnel.sensitive.read" in traded.json()["permission_codes"]
    assert "school.role.manage" not in traded.json()["permission_codes"]
    # 代价可复盘：该码进/出角色都写在 updated 审计的 added/removed 里
    last_update = (await _audit_entries("school.role.permissions.updated"))[-1]
    assert "school.personnel.sensitive.read" in last_update["metadata"]["added"]
    assert "school.role.manage" in last_update["metadata"]["removed"]


async def test_reset_preview_then_execution_restores_factory_defaults(
    client: AsyncClient,
) -> None:
    """Given 管理员把 school_admin 权限清空、范围删光；When 预览→重置出厂；Then 权限与范围一并恢复且热生效。"""

    school_code = f"SCH-RBAC{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    tenant_id = seeded["tenant_id"]
    super_headers = await login_tenant(
        client, school_code=school_code, login_id=SUPER_LOGIN, password=SUPER_PASSWORD
    )
    admin_headers = await login_tenant(
        client, school_code=school_code, login_id=ADMIN_LOGIN, password=ADMIN_PASSWORD
    )
    role_id = await _role_id(tenant_id, "school_admin")

    current = _role_payload(
        (await client.get("/api/v1/school/roles", headers=super_headers)).json(), "school_admin"
    )
    cleared = await client.put(
        f"/api/v1/school/roles/{role_id}/permissions",
        json={"permission_codes": [], "version": current["version"]},
        headers=super_headers,
    )
    assert cleared.status_code == 200, cleared.text
    cleared_version = cleared.json()["version"]
    assert cleared.json()["permission_codes"] == []
    # 数据范围不是本期编辑项：用库操作模拟运营把范围也删光
    engine = make_test_engine("admin")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("delete from data_scopes where tenant_id = :tid and role_id = :rid"),
                {"tid": str(tenant_id), "rid": str(role_id)},
            )
    finally:
        await engine.dispose()
    assert (await client.get("/api/v1/school/personnel?limit=5", headers=admin_headers)).status_code == 403

    # TD-150 复验：清空后跑一次真实同步，不会被补回（defaults_applied 已为 true）
    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await sync_rbac_definitions(session)
    finally:
        await engine.dispose()
    assert await _db_permission_codes(tenant_id, role_id) == set()
    assert await _db_role_scopes(tenant_id, role_id) == set()

    # 预览只读：给出两侧 diff 与影响用户数，不写库不审计
    preview = await client.post(
        f"/api/v1/school/roles/{role_id}/reset/preview", headers=super_headers
    )
    assert preview.status_code == 200, preview.text
    diff = preview.json()
    assert diff["version"] == cleared_version
    assert diff["permissions_added"] == sorted(SCHOOL_ADMIN_DEFAULTS)
    assert diff["permissions_removed"] == []
    assert diff["scopes_added"] == ["school"]
    assert diff["scopes_removed"] == []
    assert diff["affected_users"] == 1
    assert await _db_permission_codes(tenant_id, role_id) == set()
    assert await _db_role_scopes(tenant_id, role_id) == set()
    assert await _audit_metadata("school.role.reset") == []

    # 二次确认与理由：角色名不符/理由过短/版本过期都拒绝
    wrong_code = await client.post(
        f"/api/v1/school/roles/{role_id}/reset",
        json={
            "confirm_role_code": "school_super_admin",
            "reason": "恢复出厂默认以修复误删配置",
            "version": cleared_version,
        },
        headers=super_headers,
    )
    assert wrong_code.status_code == 422, wrong_code.text
    assert wrong_code.json()["detail"]["code"] == "ROLE_CODE_MISMATCH"
    short_reason = await client.post(
        f"/api/v1/school/roles/{role_id}/reset",
        json={"confirm_role_code": "school_admin", "reason": "误删", "version": cleared_version},
        headers=super_headers,
    )
    assert short_reason.status_code == 422, short_reason.text
    assert short_reason.json()["detail"]["code"] == "RESET_REASON_REQUIRED"
    stale = await client.post(
        f"/api/v1/school/roles/{role_id}/reset",
        json={
            "confirm_role_code": "school_admin",
            "reason": "恢复出厂默认以修复误删配置",
            "version": current["version"],
        },
        headers=super_headers,
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "ROLE_VERSION_CONFLICT"
    # 与全量替换同口径：重置的冲突也要回带最新快照（库里当前值，不是被拒的过期提交值）
    assert stale.json()["detail"]["latest"]["version"] == cleared_version
    assert await _db_permission_codes(tenant_id, role_id) == set()

    # When：真正重置（预览给出的版本 + 角色名确认 + 理由）
    reset = await client.post(
        f"/api/v1/school/roles/{role_id}/reset",
        json={
            "confirm_role_code": "school_admin",
            "reason": "恢复出厂默认以修复误删配置",
            "version": cleared_version,
        },
        headers=super_headers,
    )
    assert reset.status_code == 200, reset.text
    result = reset.json()
    assert result["version"] == cleared_version + 1
    assert result["permissions_added"] == sorted(SCHOOL_ADMIN_DEFAULTS)
    assert result["scopes_added"] == ["school"]
    assert result["affected_users"] == 1

    # Then：权限与范围都回到出厂，标记与版本同步推进
    assert await _db_permission_codes(tenant_id, role_id) == SCHOOL_ADMIN_DEFAULTS
    assert await _db_role_scopes(tenant_id, role_id) == {"school"}
    row = await _role_row(tenant_id, role_id)
    assert row["version"] == cleared_version + 1
    assert row["defaults_applied"] is True
    # 绑定用户的下一请求即恢复（会话未被踢）
    assert (
        await client.get("/api/v1/school/personnel?limit=5", headers=admin_headers)
    ).status_code == 200

    audit_rows = await _audit_metadata("school.role.reset")
    assert len(audit_rows) == 1
    metadata = audit_rows[0]
    assert metadata["role_code"] == "school_admin"
    assert metadata["permissions_added"] == sorted(SCHOOL_ADMIN_DEFAULTS)
    assert metadata["scopes_added"] == ["school"]
    assert metadata["affected_users"] == 1
    assert "修复误删配置" in metadata["reason"]

    # 幂等预览：再预览一次没有可变更项
    settled = await client.post(
        f"/api/v1/school/roles/{role_id}/reset/preview", headers=super_headers
    )
    assert settled.status_code == 200
    settled_diff = settled.json()
    assert settled_diff["permissions_added"] == []
    assert settled_diff["permissions_removed"] == []
    assert settled_diff["scopes_added"] == []
    assert settled_diff["scopes_removed"] == []
    assert settled_diff["version"] == cleared_version + 1
