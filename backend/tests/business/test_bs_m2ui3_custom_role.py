"""业务场景 BS-M2UI3-A / BS-M2UI3-E（节选）：自定义角色的创建与删除（TD-187b）。

Given 校方超管、一名尚无任何角色的馆员账号，
When 建自定义角色（初始权限或复制同租户可编辑角色）、把角色绑给人、再删掉它，
Then 每条断言都落在**库与业务结果**上，不是 HTTP 200：

- 建：`roles.is_system=false`、`defaults_applied=false`、`version=0`，`role_permissions` 逐码相等；
  列表里 `editable=true` 且无 `lock_reason`；跑一次 RBAC 同步后**分毫不动**
  （自定义角色永不参与补种，否则运营配置会被"恢复默认"）；
- 生效：把角色绑给馆员后，同一条 access token 下一请求从 403 翻成 200（能力来自库里的角色，
  不来自令牌快照）；
- 删：预览只读不审计；确认码/理由/版本三类错误各自不删数据、不推版本；内置角色 409 且留痕；
  成功那次 `role_permissions`/`data_scopes`/`user_roles` 随复合外键 CASCADE 清空，馆员下一请求
  重新 403，且审计带着**删除前**的完整权限码集（CASCADE 之后这是唯一的复盘来源）。

创建入参的边界（保留码、复制源、平台域码、转授禁令、双输入歧义）与删除权限的分离（决策 #6）
写在同一文件：它们都是"建/删角色"这一步的可观察行为，拆到别处只会把一条链路切碎。
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
from app.rbac import sync_rbac_definitions
from tests.support.auth import login_tenant, seed_tenant_user
from tests.support.db import clean_test_data, database_url, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.business, pytest.mark.integration]

SUPER_LOGIN = "cust-role-super"
SUPER_PASSWORD = "CustRole-Super-Pass-1"
KEEPER_LOGIN = "cust-role-keeper"
KEEPER_PASSWORD = "CustRole-Keeper-Pass-1"
OPERATOR_LOGIN = "cust-role-operator"
OPERATOR_PASSWORD = "CustRole-Operator-Pass-1"

# 受控读取接口 `/api/v1/school/personnel` 的守卫码：只用这一个码的有无来翻转 403/200，
# 于是断言反映的是"角色给的权限"，不掺入人员数据、密钥配置或数据范围。
CAPABILITY_CODE = "school.personnel.view"
LIBRARIAN_CODES = sorted({CAPABILITY_CODE, "school.kb.view"})
CREATE_REASON = "为图书馆岗位建角色（业务场景 BS-M2UI3-A）"
DELETION_REASON = "该职责已由新角色承接（业务场景 BS-M2UI3-E）"


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
    """超管绑 `school_super_admin`；馆员**只有账号没有角色**（自定义角色的被授权对象）。

    馆员不绑任何内置角色，所以它后续能不能读人员列表，只可能来自那个新建的自定义角色。
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
            await session.execute(
                text(
                    "insert into user_roles (tenant_id, user_id, role_id) "
                    "select :tid, :uid, r.id from roles r "
                    "where r.tenant_id = :tid and r.code = 'school_super_admin'"
                ),
                {"tid": str(tenant_id), "uid": str(super_id)},
            )
        _, keeper_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=KEEPER_LOGIN,
            password=KEEPER_PASSWORD,
            user_type="school_admin",
        )
        return {"tenant_id": tenant_id, "super_id": super_id, "keeper_id": keeper_id}
    finally:
        await engine.dispose()


async def _seed_bare_school(school_code: str) -> dict[str, Any]:
    """只建租户与内置角色（跨租断言用，不建可登录账号）。"""

    engine = make_test_engine("admin")
    try:
        tenant_id, user_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id="cust-role-foreign",
            password="CustRole-Foreign-Pass-1",
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text("select seed_builtin_roles(:tid)"), {"tid": str(tenant_id)}
            )
        return {"tenant_id": tenant_id, "user_id": user_id}
    finally:
        await engine.dispose()


async def _login_manager_only(
    client: AsyncClient, school_code: str
) -> tuple[dict[str, str], UUID]:
    """建一名持有 `school.role.manage`、**不**持有 `school.role.delete` 的管理员并登录。

    直建角色而不是让超管授权：这条用例钉的是"删除权限与配置权限分离"（决策 #6），
    授权链路本身已由 `test_bs_m2ui2_rbac_admin.py` 覆盖。
    """

    engine = make_test_engine("admin")
    try:
        _, operator_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=OPERATOR_LOGIN,
            password=OPERATOR_PASSWORD,
            user_type="school_admin",
            permissions=("school.role.manage",),
        )
    finally:
        await engine.dispose()
    headers = await login_tenant(
        client, school_code=school_code, login_id=OPERATOR_LOGIN, password=OPERATOR_PASSWORD
    )
    return headers, UUID(str(operator_id))


async def _role_id_by_code(tenant_id: UUID, code: str) -> UUID:
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


async def _sql_exec(statement: str, params: dict[str, Any]) -> None:
    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(text(statement), params)
    finally:
        await engine.dispose()


async def _bind_user_role(tenant_id: UUID, user_id: UUID, role_id: UUID) -> None:
    """用户↔角色绑定：TD-187d 才开放接口，这里走同一条真实 SQL 链路。"""

    await _sql_exec(
        "insert into user_roles (tenant_id, user_id, role_id) values (:tid, :uid, :rid)",
        {"tid": str(tenant_id), "uid": str(user_id), "rid": str(role_id)},
    )


async def _role_exists(tenant_id: UUID, role_id: UUID) -> bool:
    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            value = await session.scalar(
                text("select 1 from roles where tenant_id = :tid and id = :rid"),
                {"tid": str(tenant_id), "rid": str(role_id)},
            )
    finally:
        await engine.dispose()
    return value is not None


async def _child_row_counts(tenant_id: UUID, role_id: UUID) -> dict[str, int]:
    """三张子表的残留行数 + 内置角色绑定数：删除只该清掉这一把角色的行。"""

    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            counts: dict[str, int] = {}
            for table, statement in (
                (
                    "role_permissions",
                    "select count(*) from role_permissions "
                    "where tenant_id = :tid and role_id = :rid",
                ),
                (
                    "data_scopes",
                    "select count(*) from data_scopes "
                    "where tenant_id = :tid and role_id = :rid",
                ),
                (
                    "user_roles",
                    "select count(*) from user_roles "
                    "where tenant_id = :tid and role_id = :rid",
                ),
                (
                    "builtin_bindings",
                    "select count(*) from user_roles ur "
                    "join roles r on r.id = ur.role_id and r.tenant_id = ur.tenant_id "
                    "where ur.tenant_id = :tid and r.is_system",
                ),
            ):
                value = await session.scalar(
                    text(statement), {"tid": str(tenant_id), "rid": str(role_id)}
                )
                counts[table] = int(value or 0)
    finally:
        await engine.dispose()
    return counts


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


async def _role_row(tenant_id: UUID, role_id: UUID) -> dict[str, Any]:
    async with db_session(tenant_id=tenant_id) as session:
        row = (
            (
                await session.execute(
                    text(
                        "select code, name, is_system, defaults_applied, version from roles "
                        "where tenant_id = :tid and id = :rid"
                    ),
                    {"tid": tenant_id, "rid": role_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _audit_entries(action: str) -> list[dict[str, Any]]:
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


async def _create(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    code: str,
    name: str = "图书馆管理员",
    permission_codes: list[str] | None = None,
    copy_from_role_id: str | None = None,
    reason: str | None = None,
) -> Any:
    body: dict[str, Any] = {"code": code, "name": name}
    if permission_codes is not None:
        body["permission_codes"] = permission_codes
    if copy_from_role_id is not None:
        body["copy_from_role_id"] = copy_from_role_id
    if reason is not None:
        body["reason"] = reason
    return await client.post("/api/v1/school/roles", json=body, headers=headers)


def _detail_code(response: Any) -> str:
    return str(response.json()["detail"]["code"])


async def _delete(
    client: AsyncClient,
    headers: dict[str, str],
    role_id: UUID | str,
    *,
    confirm_role_code: str,
    reason: str = DELETION_REASON,
    version: int,
) -> Any:
    return await client.post(
        f"/api/v1/school/roles/{role_id}/delete",
        json={
            "confirm_role_code": confirm_role_code,
            "reason": reason,
            "version": version,
        },
        headers=headers,
    )


async def test_create_custom_role_grants_capability_and_survives_sync(
    client: AsyncClient,
) -> None:
    """BS-M2UI3-A：建角色 → 绑人 → 同令牌下一请求 403 翻 200；RBAC 同步不改自定义角色。"""

    school_code = f"SCH-CR{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    tenant_id = seeded["tenant_id"]
    super_headers = await login_tenant(
        client, school_code=school_code, login_id=SUPER_LOGIN, password=SUPER_PASSWORD
    )
    keeper_headers = await login_tenant(
        client, school_code=school_code, login_id=KEEPER_LOGIN, password=KEEPER_PASSWORD
    )

    # Given 馆员没有任何角色：读人员列表必然 403（能力不是从令牌里长出来的）
    assert (
        await client.get("/api/v1/school/personnel?limit=5", headers=keeper_headers)
    ).status_code == 403

    # When 超管建自定义角色
    created = await _create(
        client, super_headers, code="library_keeper", permission_codes=LIBRARIAN_CODES,
        reason=CREATE_REASON,
    )
    assert created.status_code == 201, created.text
    role = created.json()
    role_id = UUID(role["id"])
    assert role == {
        "id": str(role_id),
        "code": "library_keeper",
        "name": "图书馆管理员",
        "is_system": False,
        "editable": True,
        "lock_reason": None,
        "version": 0,
        # 自定义角色没有出厂定义：标记必须为 false，否则同步/开通会试图回填它
        "defaults_applied": False,
        "user_count": 0,
        "permission_codes": LIBRARIAN_CODES,
    }

    # Then 库里的权限映射逐码相等（断言的是"表里有什么"，不是"接口回显了什么"）
    assert await _db_permission_codes(tenant_id, role_id) == set(LIBRARIAN_CODES)
    row = await _role_row(tenant_id, role_id)
    assert row["is_system"] is False and row["defaults_applied"] is False and row["version"] == 0

    # 列表可见且可编辑（前端据此启用按钮，不自己推导语义）
    listed = next(
        item
        for item in (await client.get("/api/v1/school/roles", headers=super_headers)).json()
        if item["code"] == "library_keeper"
    )
    assert listed["editable"] is True and listed["lock_reason"] is None

    # 自定义角色在 M2-UI2 里是"待开放"（改权限 409 ROLE_IMMUTABLE），M2-UI3 起必须真的可改：
    # 新建角色若不能配权限就是死配置。这条是那次"可编辑"翻转的端到端落点。
    edited = await client.put(
        f"/api/v1/school/roles/{role_id}/permissions",
        json={
            "permission_codes": sorted({CAPABILITY_CODE}),
            "version": 0,
            "reason": "收窄到只读人员（业务场景 BS-M2UI3-A）",
        },
        headers=super_headers,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 1
    assert edited.json()["permission_codes"] == [CAPABILITY_CODE]
    assert await _db_permission_codes(tenant_id, role_id) == {CAPABILITY_CODE}
    # 改回完整集，后续断言按 LIBRARIAN_CODES 口径
    restored = await client.put(
        f"/api/v1/school/roles/{role_id}/permissions",
        json={
            "permission_codes": LIBRARIAN_CODES,
            "version": 1,
            "reason": "补回知识库可见（业务场景 BS-M2UI3-A）",
        },
        headers=super_headers,
    )
    assert restored.status_code == 200, restored.text
    assert await _db_permission_codes(tenant_id, role_id) == set(LIBRARIAN_CODES)

    # 跑一次 RBAC 同步：自定义角色**分毫不动**（补种谓词限定 `is_system`，TD-150 的边界）。
    # 若这里被同步改写，运营每接一个新版代码就丢一批自建角色的权限。
    # 版本也要原地不动：一次部署不该让所有人手里的乐观锁同时失效（满屏"请刷新重试"）。
    version_before_sync = int((await _role_row(tenant_id, role_id))["version"])
    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await sync_rbac_definitions(session)
    finally:
        await engine.dispose()
    assert await _db_permission_codes(tenant_id, role_id) == set(LIBRARIAN_CODES)
    row_after_sync = await _role_row(tenant_id, role_id)
    assert int(row_after_sync["version"]) == version_before_sync
    assert row_after_sync["defaults_applied"] is False

    # When 把角色绑给馆员（绑定接口是 TD-187d；这里走同一条 SQL 链路）
    await _bind_user_role(tenant_id, seeded["keeper_id"], role_id)

    # Then 同一条 access token 下一请求即生效，会话没有被撤销
    assert (
        await client.get("/api/v1/school/personnel?limit=5", headers=keeper_headers)
    ).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=keeper_headers)).status_code == 200
    listed_after = next(
        item
        for item in (await client.get("/api/v1/school/roles", headers=super_headers)).json()
        if item["code"] == "library_keeper"
    )
    # `user_count` 是删除影响面的前置数字，绑定后必须是 1（删角色的预览就靠这个口径）
    assert listed_after["user_count"] == 1

    created_audit = await _audit_entries("school.role.created")
    assert [entry["result"] for entry in created_audit] == ["success"]
    metadata = created_audit[0]["metadata"]
    assert created_audit[0]["resource_id"] == str(role_id)
    assert metadata["role_code"] == "library_keeper"
    assert metadata["name"] == "图书馆管理员"
    assert metadata["permission_codes"] == LIBRARIAN_CODES
    assert metadata["copy_from_role_id"] is None
    assert "BS-M2UI3-A" in str(metadata["reason"])


async def test_copy_from_and_initial_codes_are_exclusive(client: AsyncClient) -> None:
    """复制源取的是**源角色当前的实际权限集**（含运营改过的部分），且与显式清单互斥。"""

    school_code = f"SCH-CR{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    tenant_id = seeded["tenant_id"]
    super_headers = await login_tenant(
        client, school_code=school_code, login_id=SUPER_LOGIN, password=SUPER_PASSWORD
    )
    source_id = await _role_id_by_code(tenant_id, "school_admin")
    source_codes = sorted(await _db_permission_codes(tenant_id, source_id))
    assert source_codes, "复制源应带着出厂默认权限"

    copied = await _create(
        client,
        super_headers,
        code="assistant_admin",
        name="校长助理",
        copy_from_role_id=str(source_id),
        reason="以校方管理员为模板建岗位角色（业务场景 BS-M2UI3-A）",
    )
    assert copied.status_code == 201, copied.text
    copied_id = UUID(copied.json()["id"])
    assert copied.json()["permission_codes"] == source_codes
    assert await _db_permission_codes(tenant_id, copied_id) == set(source_codes)

    # 二选一：同时给清单和复制源时不能"任选一个静默执行"——调用方会以为另一份生效了
    ambiguous = await _create(
        client,
        super_headers,
        code="ambiguous_role",
        permission_codes=["school.kb.view"],
        copy_from_role_id=str(source_id),
    )
    assert ambiguous.status_code == 422, ambiguous.text
    assert _detail_code(ambiguous) == "ROLE_INITIAL_SOURCE_AMBIGUOUS"

    metadata = (await _audit_entries("school.role.created"))[0]["metadata"]
    assert metadata["copy_from_role_id"] == str(source_id)
    assert metadata["role_code"] == "assistant_admin"


async def test_reserved_code_and_cross_tenant_copy_are_rejected(
    client: AsyncClient,
) -> None:
    """保留码/同码/格式/域外码/转授禁令/跨租复制源：各自的人读错误码，且库里不落半行。"""

    school_code = f"SCH-CR{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    tenant_id = seeded["tenant_id"]
    super_headers = await login_tenant(
        client, school_code=school_code, login_id=SUPER_LOGIN, password=SUPER_PASSWORD
    )
    foreign = await _seed_bare_school(f"SCH-CRX{uuid4().hex[:6].upper()}")
    foreign_role_id = await _role_id_by_code(foreign["tenant_id"], "school_admin")

    # 内置保留码：库层 `ck_roles_builtin_code_is_system` 也会挡，接口层先给人读 409
    reserved = await _create(client, super_headers, code="student", permission_codes=[])
    assert reserved.status_code == 409, reserved.text
    assert _detail_code(reserved) == "ROLE_CODE_TAKEN"

    ok = await _create(client, super_headers, code="duty_manager", permission_codes=[])
    assert ok.status_code == 201, ok.text
    duplicate = await _create(client, super_headers, code="duty_manager", permission_codes=[])
    assert duplicate.status_code == 409, duplicate.text
    assert _detail_code(duplicate) == "ROLE_CODE_TAKEN"

    # 格式：与库层 CHECK 同界（大写/连字符/过短/过长/非 ascii 都 422，不会到 INSERT 才撞库变 500）
    for bad_code in ("Duty-Manager", "ab", "角色码中文", "a" * 64):
        rejected = await _create(client, super_headers, code=bad_code, permission_codes=[])
        assert rejected.status_code == 422, (bad_code, rejected.text)

    # 目录：幽灵码与平台域码都不能进租户角色
    ghost = await _create(
        client, super_headers, code="ghost_role_ok", permission_codes=["school.not.exists"]
    )
    assert ghost.status_code == 422, ghost.text
    assert _detail_code(ghost) == "UNKNOWN_PERMISSION_CODE"
    leaked = await _create(
        client,
        super_headers,
        code="platform_hijack",
        permission_codes=["platform.tenant.review"],
    )
    assert leaked.status_code == 422, leaked.text
    assert _detail_code(leaked) == "PLATFORM_PERMISSION_FORBIDDEN"

    # 复制源：锁定角色（超管）不能当模板；别人家的角色同形 404（不暴露存在性）
    locked_source = await _role_id_by_code(tenant_id, "school_super_admin")
    copy_locked = await _create(
        client, super_headers, code="from_super", copy_from_role_id=str(locked_source)
    )
    assert copy_locked.status_code == 422, copy_locked.text
    assert _detail_code(copy_locked) == "ROLE_COPY_SOURCE_NOT_EDITABLE"
    copy_foreign = await _create(
        client, super_headers, code="from_foreign", copy_from_role_id=str(foreign_role_id)
    )
    assert copy_foreign.status_code == 404, copy_foreign.text
    assert _detail_code(copy_foreign) == "ROLE_NOT_FOUND"

    # 库里没有多出任何一把角色：上面所有失败尝试都没落地，只有 `duty_manager` 建出来了
    codes_in_db = {
        str(item["code"])
        for item in (await client.get("/api/v1/school/roles", headers=super_headers)).json()
    }
    assert codes_in_db == {
        "school_super_admin",
        "school_admin",
        "teacher_b_end",
        "student",
        "teacher",
        "duty_manager",
    }, codes_in_db

    # 拒绝留痕只覆盖"越权/撞码/目标不可用"这一侧：格式与目录不合法属"用错了"，
    # 由 Pydantic/`_validate_codes` 直接 422，与安全拒绝分开计（同 M2-UI2 的 PUT 口径）。
    # 试图建的码写进 resource_id：此刻还没有 role id，"谁在建哪个码"就是唯一可复盘的标识。
    denied = await _audit_entries("school.role.create_denied")
    assert [
        (entry["result"], entry["resource_id"], entry["metadata"].get("reason"))
        for entry in denied
    ] == [
        ("denied", "student", "ROLE_CODE_TAKEN"),
        ("denied", "duty_manager", "ROLE_CODE_TAKEN"),
        ("denied", "from_super", "ROLE_COPY_SOURCE_NOT_EDITABLE"),
        ("denied", "from_foreign", "ROLE_NOT_FOUND"),
    ], denied

    # 跨租那条把**被探测的 role id** 也记进 metadata：`resource_id` 只是"试图建的码"，
    # 光看它回答不了"谁在拿别人家的 role id 枚举"，而这条留痕存在的理由恰恰是后者。
    assert [entry["metadata"].get("copy_from_role_id") for entry in denied] == [
        None,
        None,
        None,
        str(foreign_role_id),
    ], denied


async def test_role_manage_code_cannot_be_created_into_by_non_super(
    client: AsyncClient,
) -> None:
    """非超管建角色时带 `school.role.manage` → 422 转授禁令；超管同一码可建（矩阵 △ 格）。"""

    school_code = f"SCH-CR{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    super_headers = await login_tenant(
        client, school_code=school_code, login_id=SUPER_LOGIN, password=SUPER_PASSWORD
    )
    manager_headers, _ = await _login_manager_only(client, school_code)

    # 持有 role.manage 的人建一个**也带** role.manage 的角色 = 自我复制提权入口
    hijack = await _create(
        client,
        manager_headers,
        code="my_own_power",
        permission_codes=["school.role.manage"],
    )
    assert hijack.status_code == 422, hijack.text
    assert _detail_code(hijack) == "ROLE_CODE_NOT_DELEGABLE"

    # 同一个操作者建不含该码的角色：正常放行（禁令只针对这一个码，不是"非超管不能建角色"）
    ordinary = await _create(
        client, manager_headers, code="front_desk", permission_codes=["school.kb.view"]
    )
    assert ordinary.status_code == 201, ordinary.text
    assert await _db_permission_codes(seeded["tenant_id"], UUID(ordinary.json()["id"])) == {
        "school.kb.view"
    }

    super_copy = await _create(
        client, super_headers, code="deputy", permission_codes=["school.role.manage"]
    )
    assert super_copy.status_code == 201, super_copy.text

    denied = await _audit_entries("school.role.create_denied")
    assert [(e["result"], e["metadata"].get("reason")) for e in denied] == [
        ("denied", "ROLE_CODE_NOT_DELEGABLE")
    ], denied


async def test_delete_requires_its_own_permission_code(client: AsyncClient) -> None:
    """决策 #6：能配权限 ≠ 能删角色。持有 `school.role.manage` 者删除 → 403，角色仍在。"""

    school_code = f"SCH-CR{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    tenant_id = seeded["tenant_id"]
    super_headers = await login_tenant(
        client, school_code=school_code, login_id=SUPER_LOGIN, password=SUPER_PASSWORD
    )
    created = await _create(
        client, super_headers, code="doomed_role", permission_codes=LIBRARIAN_CODES
    )
    role_id = UUID(created.json()["id"])
    manager_headers, _ = await _login_manager_only(client, school_code)

    # 预览是只读，走 role.manage 即可（与重置预览同口径）
    preview = await client.post(
        f"/api/v1/school/roles/{role_id}/delete/preview", headers=manager_headers
    )
    assert preview.status_code == 200, preview.text
    assert preview.json() == {
        "role_code": "doomed_role",
        "name": "图书馆管理员",
        "version": 0,
        "affected_users": 0,
        "permission_codes": LIBRARIAN_CODES,
    }

    denied = await _delete(
        client, manager_headers, role_id, confirm_role_code="doomed_role", version=0
    )
    assert denied.status_code == 403, denied.text
    # 服务端门禁，不是前端按钮禁用：角色与其权限映射都还在
    assert await _role_exists(tenant_id, role_id)
    assert await _db_permission_codes(tenant_id, role_id) == set(LIBRARIAN_CODES)
    assert await _audit_entries("school.role.deleted") == []
    # 403 由鉴权依赖抛出，到不了服务层——与 personnal 侧同口径，不额外落角色审计
    assert await _audit_entries("school.role.delete_denied") == []


async def test_delete_custom_role_cascades_and_snapshots_into_audit(
    client: AsyncClient,
) -> None:
    """BS-M2UI3-E：删除自定义角色 → CASCADE 清三张子表、能力即刻消失、审计带删除前码集。"""

    school_code = f"SCH-CR{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    tenant_id = seeded["tenant_id"]
    super_headers = await login_tenant(
        client, school_code=school_code, login_id=SUPER_LOGIN, password=SUPER_PASSWORD
    )
    keeper_headers = await login_tenant(
        client, school_code=school_code, login_id=KEEPER_LOGIN, password=KEEPER_PASSWORD
    )
    created = await _create(
        client, super_headers, code="temp_keeper", permission_codes=LIBRARIAN_CODES
    )
    role_id = UUID(created.json()["id"])
    await _bind_user_role(tenant_id, seeded["keeper_id"], role_id)
    # 数据范围行（TD-187c 才开放接口，这里直接建）：验证它也随 CASCADE 消失
    await _sql_exec(
        "insert into data_scopes (tenant_id, role_id, scope_type, scope_id) "
        "values (:tid, :rid, 'school', null)",
        {"tid": str(tenant_id), "rid": str(role_id)},
    )
    assert (
        await client.get("/api/v1/school/personnel?limit=5", headers=keeper_headers)
    ).status_code == 200

    # 先把版本推到 2 再删：只删 `version=0` 的新角色，"返回被删行的最后版本号"与"恒返回 0"
    # 在断言里分不开，等于没钉住这条语义。两次 PUT 一收一放，最终码集仍是 LIBRARIAN_CODES。
    for step, payload_codes in ((0, sorted({CAPABILITY_CODE})), (1, LIBRARIAN_CODES)):
        bumped = await client.put(
            f"/api/v1/school/roles/{role_id}/permissions",
            json={
                "permission_codes": payload_codes,
                "version": step,
                "reason": "把版本推到 2，删除响应才有可区分的版本号（业务场景 BS-M2UI3-E）",
            },
            headers=super_headers,
        )
        assert bumped.status_code == 200, bumped.text
    assert bumped.json()["version"] == 2

    # 预览：只读、不审计、不改任何东西
    preview = await client.post(
        f"/api/v1/school/roles/{role_id}/delete/preview", headers=super_headers
    )
    assert preview.status_code == 200, preview.text
    assert preview.json() == {
        "role_code": "temp_keeper",
        "name": "图书馆管理员",
        "version": 2,
        "affected_users": 1,
        "permission_codes": LIBRARIAN_CODES,
    }
    assert await _audit_entries("school.role.deleted") == []
    assert await _role_exists(tenant_id, role_id)

    # 确认码打错 → 422，不删（"用错了"，不落安全审计）
    mismatch = await _delete(
        client, super_headers, role_id, confirm_role_code="temp-keeper", version=2
    )
    assert mismatch.status_code == 422, mismatch.text
    assert _detail_code(mismatch) == "ROLE_CODE_MISMATCH"
    assert await _role_exists(tenant_id, role_id)

    # 理由太短 → 422（服务层再校验，Pydantic 只保证非空）
    short_reason = await _delete(
        client,
        super_headers,
        role_id,
        confirm_role_code="temp_keeper",
        reason="不用了",
        version=2,
    )
    assert short_reason.status_code == 422, short_reason.text
    assert _detail_code(short_reason) == "ROLE_DELETE_REASON_REQUIRED"

    # 版本过期 → 409 + 最新快照，不删
    stale = await _delete(
        client, super_headers, role_id, confirm_role_code="temp_keeper", version=7
    )
    assert stale.status_code == 409, stale.text
    assert _detail_code(stale) == "ROLE_VERSION_CONFLICT"
    assert stale.json()["detail"]["latest"]["version"] == 2
    assert await _role_exists(tenant_id, role_id)

    # 内置角色不可删：409 且留痕（只能重置或改配置）
    builtin_id = await _role_id_by_code(tenant_id, "school_admin")
    builtin_delete = await _delete(
        client, super_headers, builtin_id, confirm_role_code="school_admin", version=0
    )
    assert builtin_delete.status_code == 409, builtin_delete.text
    assert _detail_code(builtin_delete) == "ROLE_NOT_DELETABLE"
    assert await _role_exists(tenant_id, builtin_id)

    # 不存在 / 别人家的同名角色：同形 404，不区分「没有」与「不是你的」
    missing_id = uuid4()
    missing_delete = await _delete(
        client, super_headers, missing_id, confirm_role_code="ghost", version=0
    )
    assert missing_delete.status_code == 404, missing_delete.text
    foreign = await _seed_bare_school(f"SCH-CRY{uuid4().hex[:6].upper()}")
    foreign_role_id = await _role_id_by_code(foreign["tenant_id"], "school_admin")
    foreign_delete = await _delete(
        client, super_headers, foreign_role_id, confirm_role_code="school_admin", version=0
    )
    assert foreign_delete.status_code == 404, foreign_delete.text
    assert await _role_exists(foreign["tenant_id"], foreign_role_id)

    # When 正式删除
    deleted = await _delete(
        client, super_headers, role_id, confirm_role_code="temp_keeper", version=2
    )
    assert deleted.status_code == 200, deleted.text
    # 与预览同形同值：`version` 是被删行的**最后版本号**，不是 `+1`——行已不在了，
    # 造一个从未存在过的版本只会诱导前端拿它当下一次乐观锁的基线。
    assert deleted.json() == preview.json(), (deleted.json(), preview.json())

    # Then 角色行消失，三张子表随复合外键 CASCADE 清空；内置角色的绑定不受影响
    assert not await _role_exists(tenant_id, role_id)
    counts = await _child_row_counts(tenant_id, role_id)
    assert counts["role_permissions"] == 0, counts
    assert counts["data_scopes"] == 0, counts
    assert counts["user_roles"] == 0, counts
    assert counts["builtin_bindings"] == 1, counts

    # 能力即刻消失（同一条 token；没有缓存把已删角色的权限留在会话里）
    assert (
        await client.get("/api/v1/school/personnel?limit=5", headers=keeper_headers)
    ).status_code == 403
    assert (await client.get("/api/v1/auth/me", headers=keeper_headers)).status_code == 200

    deletion_audit = await _audit_entries("school.role.deleted")
    assert [entry["result"] for entry in deletion_audit] == ["success"]
    metadata = deletion_audit[0]["metadata"]
    assert deletion_audit[0]["resource_id"] == str(role_id)
    assert metadata["role_code"] == "temp_keeper"
    assert metadata["name"] == "图书馆管理员"
    # 删除前的完整码集快照：CASCADE 之后这是唯一能回答"这个角色当时能干什么"的地方
    assert metadata["removed_permission_codes"] == LIBRARIAN_CODES
    assert metadata["affected_users"] == 1
    assert "BS-M2UI3-E" in str(metadata["reason"])

    denied = await _audit_entries("school.role.delete_denied")
    assert [
        (entry["result"], entry["metadata"].get("reason")) for entry in denied
    ] == [
        ("denied", "ROLE_NOT_DELETABLE"),
        ("denied", "ROLE_NOT_FOUND"),
        ("denied", "ROLE_NOT_FOUND"),
    ], denied
    assert [entry["resource_id"] for entry in denied] == [
        str(builtin_id),
        str(missing_id),
        str(foreign_role_id),
    ], denied
