"""M1-E2 集成测试：数据范围服务端过滤、越权口径与平台跨租只读通道。

口径（`docs/design-briefs/M1-E2-data-scope.md` §0 ①②④⑥⑨⑩）：

- 范围来自 `data_scopes`（角色 + 用户并集），**在仓储基类注入**，接口层不参与；
- 列表静默过滤；显式 id 越权 → 404 且写 `data_scope.denied` 审计；
- 解析不到范围 = 空集（fail-closed），不是"全校"；
- 跨租通道：平台权限 + 必填理由 + **只读事务** + 强制审计，一次一个租户。
"""

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth import revocation
from app.auth.principal import Principal
from app.config import get_settings
from app.db.session import dispose_engine
from app.main import create_app
from app.personnel.cross_tenant import (
    CrossTenantError,
    _read_only_tenant_session,
    read_tenant_personnel,
)
from tests.support.auth import (
    login_platform,
    login_tenant,
    seed_platform_user,
    seed_tenant_user,
)
from tests.support.db import (
    clean_test_data,
    database_url,
    make_session_factory,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.integration]

VIEW = "school.personnel.view"
# 口径 ⑥ / 简报 §2-C 固定的响应形状：多一个字段（尤其 `login_id`）就是用例失败。
_RESPONSE_KEYS = frozenset(
    {"id", "user_type", "display_name", "status", "last_login_at", "email_masked",
     "id_card_mask"}
)
SCHOOL_A = "SCH-E2-A"
SCHOOL_B = "SCH-E2-B"
PASSWORD = "Scope-Test-Pass-1"

# A 校 5 个用户 = 4 种「范围 × 权限」组合 + 1 个普通学生；B 校 1 个用户用于跨租不可见
A_SCHOOL_VIEWER = "A-SCHOOL"
A_SELF_VIEWER = "A-SELF"
A_NO_SCOPE = "A-NONE"
A_NO_PERM = "A-NOPERM"
A_PLAIN_STUDENT = "A-STUDENT"
B_STUDENT = "B-STUDENT"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    """确保测试库结构最新（并做一次性测试库护栏）。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def env(migrated_database: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """清空业务数据、种两所学校的人员与平台账号，并把应用指向测试栈。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
        await _seed_data(admin_engine)
    finally:
        await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    get_settings.cache_clear()
    await revocation.reset_client()

    yield

    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()


async def _seed_data(admin_engine: AsyncEngine) -> None:
    for login_id, scope_types, permissions in (
        (A_SCHOOL_VIEWER, ("school",), (VIEW,)),
        (A_SELF_VIEWER, ("self",), (VIEW,)),
        (A_NO_SCOPE, (), (VIEW,)),
        (A_NO_PERM, ("school",), ()),
        (A_PLAIN_STUDENT, ("self",), ()),
    ):
        await seed_tenant_user(
            admin_engine,
            school_code=SCHOOL_A,
            login_id=login_id,
            password=PASSWORD,
            user_type="student",
            email=f"{login_id.lower()}@school-a.test",
            permissions=permissions,
            scope_types=scope_types,
        )
    await seed_tenant_user(
        admin_engine,
        school_code=SCHOOL_B,
        login_id=B_STUDENT,
        password=PASSWORD,
        user_type="student",
        email="b@student.test",
        permissions=(VIEW,),
        scope_types=("school",),
    )
    await seed_platform_user(
        admin_engine, account="e2-reader", permissions=("platform.cross_tenant.read",)
    )
    await seed_platform_user(admin_engine, account="e2-plain", permissions=("platform.tenant.view",))


@pytest.fixture
async def client(env: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _login(client: AsyncClient, login_id: str) -> dict[str, str]:
    return await login_tenant(client, school_code=SCHOOL_A, login_id=login_id, password=PASSWORD)


async def _scalar_rows(sql: str, **params: object) -> list[tuple[object, ...]]:
    """管理连接上执行一条只读 SQL（测试内的取数助手，不经过应用会话）。"""

    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            rows = (await session.execute(text(sql), params)).all()
        return [tuple(row) for row in rows]
    finally:
        await engine.dispose()


async def _tenant_ids(school_code: str) -> UUID:
    rows = await _scalar_rows(
        "select id from tenants where school_code = :code", code=school_code
    )
    assert len(rows) == 1
    return UUID(str(rows[0][0]))


async def _user_ids_of(school_code: str) -> set[UUID]:
    rows = await _scalar_rows(
        "select u.id from users u join tenants t on t.id = u.tenant_id "
        "where t.school_code = :code",
        code=school_code,
    )
    return {UUID(str(row[0])) for row in rows}


async def _id_of_login(login_id: str) -> UUID:
    rows = await _scalar_rows(
        "select u.id from users u join tenants t on t.id = u.tenant_id "
        "where u.login_id = :login_id and t.school_code = :code",
        login_id=login_id,
        code=SCHOOL_A,
    )
    assert len(rows) == 1
    return UUID(str(rows[0][0]))


async def _audits(action: str) -> list[dict[str, object]]:
    rows = await _scalar_rows(
        "select resource_id, result, metadata_json from audit_logs where action = :action "
        "order by sequence",
        action=action,
    )
    return [{"resource_id": r[0], "result": r[1], "metadata": r[2]} for r in rows]


@pytest.mark.asyncio
async def test_school_scope_sees_whole_tenant_and_nothing_else(client: AsyncClient) -> None:
    """全校范围：看到本校全部用户，看不到别的学校（范围 + 租户过滤 + RLS 三层同向）。"""

    headers = await _login(client, A_SCHOOL_VIEWER)
    expected = await _user_ids_of(SCHOOL_A)
    other_school = await _user_ids_of(SCHOOL_B)

    response = await client.get("/api/v1/school/personnel?limit=100", headers=headers)
    assert response.status_code == 200, response.text
    ids = {UUID(item["id"]) for item in response.json()}
    assert ids == expected
    assert ids.isdisjoint(other_school)


@pytest.mark.asyncio
async def test_self_scope_sees_only_the_caller(client: AsyncClient) -> None:
    """仅本人范围：列表只剩自己那一行，且响应形状不越过口径 ⑥。"""

    headers = await _login(client, A_SELF_VIEWER)
    response = await client.get("/api/v1/school/personnel?limit=100", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    assert UUID(body[0]["id"]) == await _id_of_login(A_SELF_VIEWER)
    assert set(body[0]) == _RESPONSE_KEYS


@pytest.mark.asyncio
async def test_response_shape_excludes_login_id(client: AsyncClient) -> None:
    """口径 ⑥ 的硬断言：`login_id`（学号/工号）不在人员列表的任何一行里。

    为什么单独一条：`login_id` 是校方内部主标识，列表接口只需要 id 就能定位；
    把它放进响应等于给每个持 `school.personnel.view` 的角色一份可导出的名册。
    M2-ORG04：响应形状新增 `id_card_mask`（掩码展示值，无明文/密文/HMAC 泄漏）。"""

    headers = await _login(client, A_SCHOOL_VIEWER)
    response = await client.get("/api/v1/school/personnel?limit=100", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body, "名单非空才有意义"
    assert all(set(item) == _RESPONSE_KEYS for item in body)
    assert "login_id" not in response.text
    assert "password" not in response.text.lower()


@pytest.mark.asyncio
async def test_missing_scope_rows_returns_empty_not_everything(client: AsyncClient) -> None:
    """没有任何范围行 → 空集（fail-closed），而不是退化成全校可见。"""

    headers = await _login(client, A_NO_SCOPE)
    response = await client.get("/api/v1/school/personnel?limit=100", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == []


@pytest.mark.asyncio
async def test_email_is_masked_in_list(client: AsyncClient) -> None:
    """列表里的邮箱必须脱敏（红线：完整值需权限 + 理由 + 审计，本期不提供该路径）。"""

    headers = await _login(client, A_SCHOOL_VIEWER)
    response = await client.get("/api/v1/school/personnel?limit=100", headers=headers)
    assert response.status_code == 200, response.text
    masked = {item["email_masked"] for item in response.json()}
    assert masked, "列表不应为空"
    assert all("***" in value for value in masked if value)
    assert "a-school@school-a.test" not in masked


@pytest.mark.asyncio
async def test_detail_of_visible_person_returns_profile(client: AsyncClient) -> None:
    """范围内的人：详情返回脱敏档案。"""

    headers = await _login(client, A_SCHOOL_VIEWER)
    target = await _id_of_login(A_PLAIN_STUDENT)
    response = await client.get(f"/api/v1/school/personnel/{target}", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert UUID(body["id"]) == target
    assert set(body) == _RESPONSE_KEYS
    assert "***" in body["email_masked"]


@pytest.mark.asyncio
async def test_detail_out_of_scope_is_404_and_audited(client: AsyncClient) -> None:
    """越权详情：404（不是 403——403 等于承认"这个人存在"）+ `data_scope.denied` 审计。"""

    headers = await _login(client, A_SELF_VIEWER)
    listed = (await client.get("/api/v1/school/personnel?limit=100", headers=headers)).json()
    assert [UUID(item["id"]) for item in listed] == [await _id_of_login(A_SELF_VIEWER)]

    target = await _id_of_login(A_PLAIN_STUDENT)
    response = await client.get(f"/api/v1/school/personnel/{target}", headers=headers)
    assert response.status_code == 404, response.text

    audits = await _audits("data_scope.denied")
    assert len(audits) == 1, audits
    row = audits[0]
    assert row["resource_id"] == str(target)
    assert row["result"] == "denied"
    metadata = row["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["reason"] == "out_of_data_scope"
    assert list(metadata["scope_types"]) == ["self"]


@pytest.mark.asyncio
async def test_detail_of_nonexistent_person_is_404_without_denial_audit(
    client: AsyncClient,
) -> None:
    """不存在的人：同样 404，但不写越权审计——能区分这两件事，那条审计才有价值。"""

    headers = await _login(client, A_SELF_VIEWER)
    response = await client.get(f"/api/v1/school/personnel/{uuid4()}", headers=headers)
    assert response.status_code == 404, response.text
    assert await _audits("data_scope.denied") == []


@pytest.mark.asyncio
async def test_missing_permission_is_rejected(client: AsyncClient) -> None:
    """没有 `school.personnel.view`：403 + 权限拒绝审计（数据范围不是权限的替代品）。"""

    headers = await _login(client, A_NO_PERM)
    response = await client.get("/api/v1/school/personnel", headers=headers)
    assert response.status_code == 403, response.text
    denied = await _audits("auth.permission.denied")
    assert [row["resource_id"] for row in denied] == [VIEW]


@pytest.mark.asyncio
async def test_cross_tenant_read_returns_target_tenant_rows_and_audits(
    client: AsyncClient,
) -> None:
    """跨租只读：带理由读取目标学校人员，并留下可读回的理由审计。"""

    headers = await login_platform(client, account="e2-reader")
    tenant_a = await _tenant_ids(SCHOOL_A)
    expected = await _user_ids_of(SCHOOL_A)

    response = await client.post(
        "/api/v1/platform/cross-tenant/personnel/read",
        json={"tenant_id": str(tenant_a), "reason": "年度账号清点（审计工单 AUD-7）"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert {UUID(item["id"]) for item in body["items"]} == expected
    assert all("***" in (item["email_masked"] or "***") for item in body["items"])

    audits = await _audits("platform.cross_tenant.read")
    assert len(audits) == 1
    metadata = audits[0]["metadata"]
    assert isinstance(metadata, dict)
    assert "年度账号清点" in metadata["reason"]
    assert metadata["row_count"] == len(expected)


@pytest.mark.asyncio
async def test_cross_tenant_read_requires_platform_realm(client: AsyncClient) -> None:
    """租户主体不能借道跨租通道（realm 边界先于权限判定）。"""

    headers = await _login(client, A_SCHOOL_VIEWER)
    tenant_a = await _tenant_ids(SCHOOL_A)
    response = await client.post(
        "/api/v1/platform/cross-tenant/personnel/read",
        json={"tenant_id": str(tenant_a), "reason": "越界尝试"},
        headers=headers,
    )
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_cross_tenant_read_requires_permission(client: AsyncClient) -> None:
    """平台账号但没有 `platform.cross_tenant.read`：403，且没有任何跨租审计。"""

    headers = await login_platform(client, account="e2-plain")
    tenant_a = await _tenant_ids(SCHOOL_A)
    response = await client.post(
        "/api/v1/platform/cross-tenant/personnel/read",
        json={"tenant_id": str(tenant_a), "reason": "想看"},
        headers=headers,
    )
    assert response.status_code == 403, response.text
    assert await _audits("platform.cross_tenant.read") == []


@pytest.mark.asyncio
async def test_cross_tenant_read_requires_reason(client: AsyncClient) -> None:
    """理由缺失/空白：422（必填理由是这条通道成立的前提）。"""

    headers = await login_platform(client, account="e2-reader")
    tenant_a = await _tenant_ids(SCHOOL_A)
    blank = await client.post(
        "/api/v1/platform/cross-tenant/personnel/read",
        json={"tenant_id": str(tenant_a), "reason": "   "},
        headers=headers,
    )
    missing = await client.post(
        "/api/v1/platform/cross-tenant/personnel/read",
        json={"tenant_id": str(tenant_a)},
        headers=headers,
    )
    assert blank.status_code == 422, blank.text
    assert missing.status_code == 422, missing.text
    assert await _audits("platform.cross_tenant.read") == []


@pytest.mark.asyncio
async def test_cross_tenant_read_fails_when_audit_cannot_be_written(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审计写不进去 → 请求必须失败，且**不把任何档案交给调用方**（fail-closed）。

    为什么单独一条：这条通道的合法性全靠"事后能回答谁看了哪个学校"。如果审计失败还继续
    返回 200，就出现了一次不可审计的跨租读取——比 403 更糟，因为它没有痕迹。
    """

    from app.personnel import cross_tenant

    headers = await login_platform(client, account="e2-reader")
    tenant_a = await _tenant_ids(SCHOOL_A)

    async def _audit_down(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(cross_tenant, "append_audit", _audit_down)

    with pytest.raises(RuntimeError):
        # ASGITransport 不吞未处理异常：服务层故障会原样抛出（生产由 Nginx/ASGI 转成 5xx）
        await client.post(
            "/api/v1/platform/cross-tenant/personnel/read",
            json={"tenant_id": str(tenant_a), "reason": "审计故障下的读取"},
            headers=headers,
        )
    assert await _audits("platform.cross_tenant.read") == []


@pytest.mark.asyncio
async def test_cross_tenant_read_rejects_unknown_tenant(client: AsyncClient) -> None:
    """目标租户不存在：404，且不留跨租读取审计（没读成功就不该有"看过"的痕迹）。"""

    headers = await login_platform(client, account="e2-reader")
    response = await client.post(
        "/api/v1/platform/cross-tenant/personnel/read",
        json={"tenant_id": str(uuid4()), "reason": "试一个不存在的学校"},
        headers=headers,
    )
    assert response.status_code == 404, response.text
    assert await _audits("platform.cross_tenant.read") == []


@pytest.mark.asyncio
async def test_cross_tenant_session_is_read_only_at_database_level(env: None) -> None:
    """只读收口在数据库层：在这条通道的事务里写一行都会被 PG 拒绝。

    这是"跨租通道不允许放宽写权限"（简报 §8）的硬证明——不依赖代码自觉。
    """

    tenant_a = await _tenant_ids(SCHOOL_A)
    async with _read_only_tenant_session(tenant_a) as session:
        with pytest.raises(DBAPIError) as excinfo:
            await session.execute(
                text("insert into users (tenant_id, user_type, login_id, password_hash) "
                     "values (:tid, 'student', 'RO-PROBE', 'x')"),
                {"tid": str(tenant_a)},
            )
    assert "read-only transaction" in str(excinfo.value).lower()
    # 探针写入的会话已随异常回滚，不留残留行
    rows = await _scalar_rows("select 1 from users where login_id = 'RO-PROBE'")
    assert rows == []


@pytest.mark.asyncio
async def test_read_tenant_personnel_rejects_non_platform_principal(env: None) -> None:
    """服务层同样拒绝租户主体（接口层 403 之外还有第二道，防止别的调用方绕过路由）。"""

    tenant_a = await _tenant_ids(SCHOOL_A)
    principal = Principal(
        user_id=uuid4(),
        realm="tenant",
        tenant_id=tenant_a,
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=datetime.now(UTC),
        must_change_password=False,
        roles=frozenset(),
        permissions=frozenset({"platform.cross_tenant.read"}),
    )
    with pytest.raises(CrossTenantError) as excinfo:
        await read_tenant_personnel(
            principal, tenant_id=tenant_a, reason="试图绕过 realm 检查"
        )
    assert excinfo.value.code == "REALM_MISMATCH"
    assert excinfo.value.http_status == 403
