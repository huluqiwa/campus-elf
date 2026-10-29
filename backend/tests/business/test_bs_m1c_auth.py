"""业务场景 BS-M1-C：三端登录、首登改密门禁、刷新重用、登出即时失效、平台审核鉴权。"""

import os
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.auth import revocation
from app.auth.passwords import hash_password
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.main import create_app
from tests.support.auth import TEST_PLATFORM_PASSWORD, login_platform, seed_platform_user
from tests.support.db import (
    clean_test_data,
    database_url,
    make_session_factory,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.business, pytest.mark.integration]

TENANT_PASSWORD = "Student-Init-Pass-1"
NEW_PASSWORD = "Student-New-Pass-22"
SCHOOL_CODE = "SCH-BSAUTH01"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    """迁移测试库；对清理连接做一次性库护栏。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def client(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    """指向测试库/Redis 的应用客户端，并准备好平台账号与租户用户。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
        factory = make_session_factory(admin_engine)
        async with factory() as session, session.begin():
            tenant_id = await session.scalar(
                text(
                    "insert into tenants (school_code, name, status, activated_at) "
                    "values (:code, '业务认证中学', 'active', now()) returning id"
                ),
                {"code": SCHOOL_CODE},
            )
            await session.execute(
                text(
                    "insert into users (tenant_id, user_type, login_id, display_name, "
                    "password_hash, status, must_change_password, mfa_enabled) "
                    "values (:tenant_id, 'student', 'S-9001', '业务学生', :hash, 'active', "
                    "true, false)"
                ),
                {"tenant_id": tenant_id, "hash": hash_password(TENANT_PASSWORD)},
            )
        await seed_platform_user(
            admin_engine,
            account="ops-full",
            permissions=("platform.tenant.review", "platform.tenant.view"),
        )
        await seed_platform_user(
            admin_engine, account="ops-view-only", permissions=("platform.tenant.view",)
        )
    finally:
        await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    # 测试栈 SMTP/MailHog 与本地对象存储：不依赖 127.0.0.1:1025 等本机默认端口
    monkeypatch.setenv("SMTP_HOST", os.environ["TEST_SMTP_HOST"])
    monkeypatch.setenv("SMTP_PORT", os.environ["TEST_SMTP_PORT"])
    monkeypatch.setenv("SMTP_FROM", "no-reply@campus-elf.test")
    object_dir = Path(os.environ["TEST_OBJECT_STORE_DIR"]) / uuid4().hex
    object_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OBJECT_STORE_BACKEND", "local")
    monkeypatch.setenv("OBJECT_STORE_DIR", str(object_dir))
    monkeypatch.setenv("DEV_SKIP_SCHOOL_REVIEW", "false")
    get_settings.cache_clear()
    await revocation.reset_client()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()
    shutil.rmtree(object_dir, ignore_errors=True)


async def _tenant_login(client: AsyncClient, *, password: str = TENANT_PASSWORD) -> dict[str, object]:
    """C 端学生登录，返回响应体。"""

    response = await client.post(
        "/api/v1/auth/login",
        json={
            "realm": "tenant",
            "school_code": SCHOOL_CODE,
            "login_id": "S-9001",
            "password": password,
        },
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _audit_actions() -> list[str]:
    """读取审计动作列表。"""

    async with db_session(platform=True) as session:
        return list((await session.execute(text("select action from audit_logs"))).scalars().all())


async def _audit_metadata(action: str) -> list[dict[str, object]]:
    """按时间顺序读取某动作的审计 metadata。"""

    async with db_session(platform=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select metadata_json from audit_logs where action = :action "
                        "order by created_at, event_id"
                    ),
                    {"action": action},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row["metadata_json"] or {}) for row in rows]


async def test_bs_m1c_first_login_password_gate_and_change(client: AsyncClient) -> None:
    """首登必须改密：门禁拦截业务接口 → 改密 → 新令牌可用、旧令牌与刷新令牌立即失效。"""

    body = await _tenant_login(client)
    access = str(body["access_token"])
    refresh = str(body["refresh_token"])
    assert body["must_change_password"] is True

    headers = {"Authorization": f"Bearer {access}"}

    # 门禁期间：/auth/me 可用，平台业务接口被拒
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["realm"] == "tenant"

    gated = await client.get("/api/v1/platform/tenants", headers=headers)
    assert gated.status_code == 403
    assert "修改密码" in gated.json()["detail"]

    changed = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": TENANT_PASSWORD, "new_password": NEW_PASSWORD},
        headers=headers,
    )
    assert changed.status_code == 200, changed.text
    new_access = changed.json()["access_token"]
    assert changed.json()["must_change_password"] is False

    # 旧访问令牌因 token_version 自增立即失效
    stale = await client.get("/api/v1/auth/me", headers=headers)
    assert stale.status_code == 401

    # 旧刷新令牌已随改密撤销
    old_refresh = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert old_refresh.status_code == 401

    fresh_me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {new_access}"}
    )
    assert fresh_me.status_code == 200
    assert fresh_me.json()["must_change_password"] is False

    actions = await _audit_actions()
    assert "auth.login.success" in actions
    assert "auth.password.changed" in actions

    # 布尔语义改用新键 `sessions_all_revoked`；`sessions_revoked` 现在只放整数条数（TD-231）
    changed_metadata = (await _audit_metadata("auth.password.changed"))[-1]
    assert changed_metadata["sessions_all_revoked"] is True
    assert "sessions_revoked" not in changed_metadata


async def test_bs_m1c_refresh_reuse_revokes_family(client: AsyncClient) -> None:
    """刷新轮换后旧令牌重用：401、整个家族撤销、审计留痕。"""

    body = await _tenant_login(client)
    first_refresh = str(body["refresh_token"])

    rotated = await client.post("/api/v1/auth/refresh", json={"refresh_token": first_refresh})
    assert rotated.status_code == 200, rotated.text
    second_refresh = rotated.json()["refresh_token"]
    assert second_refresh != first_refresh

    reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": first_refresh})
    assert reuse.status_code == 401

    after_reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": second_refresh})
    assert after_reuse.status_code == 401

    assert "auth.refresh.reuse_detected" in await _audit_actions()


async def test_bs_m1c_logout_invalidates_access_and_refresh(client: AsyncClient) -> None:
    """登出：刷新家族撤销，且当前访问令牌立即失效（不等待 15 分钟）。"""

    body = await _tenant_login(client)
    access = str(body["access_token"])
    refresh = str(body["refresh_token"])
    headers = {"Authorization": f"Bearer {access}"}

    logout = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": refresh}, headers=headers
    )
    assert logout.status_code == 200

    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401
    assert (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    ).status_code == 401
    assert "auth.logout" in await _audit_actions()


async def test_bs_m1c_platform_review_requires_permission_and_audits(
    client: AsyncClient,
) -> None:
    """平台审核接口：有权限可审核（审计含真实审核人）；无权限 403 并写拒绝审计。"""

    # 无权限平台账号
    view_only_headers = await login_platform(client, "ops-view-only")
    forbidden = await client.get("/api/v1/platform/tenant-requests", headers=view_only_headers)
    assert forbidden.status_code == 200  # view 权限足够列表

    # 注册一所需审核的学校
    register = await client.post(
        "/api/v1/platform/tenants/register",
        data={
            "school_name": "认证业务中学",
            "address": "认证路 1 号",
            "leadership": "认证校长",
            "email": "auth-review@school.edu",
        },
        files={
            "qualification": (
                "q.pdf",
                b"%PDF-1.4\n% auth business\n%%EOF\n",
                "application/pdf",
            )
        },
    )
    assert register.status_code == 201, register.text
    request_id = register.json()["request_id"]

    denied = await client.post(
        f"/api/v1/platform/tenant-requests/{request_id}/review",
        json={"result": "approved"},
        headers=view_only_headers,
    )
    assert denied.status_code == 403

    full_headers = await login_platform(client, "ops-full")
    approved = await client.post(
        f"/api/v1/platform/tenant-requests/{request_id}/review",
        json={"result": "approved", "reason": "资质齐全"},
        headers=full_headers,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    actions = await _audit_actions()
    assert "auth.permission.denied" in actions
    assert "tenant.approve" in actions

    async with db_session(platform=True) as session:
        actor_ids: list[object] = list(
            (
                await session.execute(
                    text("select actor_id from audit_logs where action = 'tenant.approve'")
                )
            )
            .scalars()
            .all()
        )
    assert actor_ids and all(actor is not None for actor in actor_ids)


async def test_bs_m1c_cross_realm_is_rejected(client: AsyncClient) -> None:
    """跨 realm：租户令牌访问平台接口 403；平台令牌不带 tenant_id；伪造 realm 令牌被拒。"""

    tenant_body = await _tenant_login(client)
    tenant_headers = {"Authorization": f"Bearer {tenant_body['access_token']}"}

    rejected = await client.get("/api/v1/platform/tenants", headers=tenant_headers)
    assert rejected.status_code == 403

    platform_headers = await login_platform(client, "ops-full")
    me = await client.get("/api/v1/auth/me", headers=platform_headers)
    assert me.status_code == 200
    payload = me.json()
    assert payload["realm"] == "platform"
    assert payload["tenant_id"] is None
    assert "platform.tenant.review" in payload["permissions"]

    # 篡改 payload 的令牌（签名失效）→ 401
    token = str(tenant_body["access_token"])
    tampered = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tampered}"})
    ).status_code == 401


async def test_bs_m1c_login_failure_message_is_uniform(client: AsyncClient) -> None:
    """未知学校、错误密码、未知账号返回同一文案，且失败写审计。"""

    wrong_password = await client.post(
        "/api/v1/auth/login",
        json={
            "realm": "tenant",
            "school_code": SCHOOL_CODE,
            "login_id": "S-9001",
            "password": "definitely-wrong",
        },
    )
    unknown_school = await client.post(
        "/api/v1/auth/login",
        json={
            "realm": "tenant",
            "school_code": "SCH-NOT-EXIST",
            "login_id": "S-9001",
            "password": TENANT_PASSWORD,
        },
    )
    wrong_platform = await client.post(
        "/api/v1/auth/login",
        json={"realm": "platform", "account": "ops-full", "password": "definitely-wrong"},
    )

    assert wrong_password.status_code == unknown_school.status_code == 401
    assert wrong_password.json()["detail"] == unknown_school.json()["detail"]
    assert wrong_platform.status_code == 401
    assert "auth.login.failed" in await _audit_actions()


async def test_bs_m1c_platform_password_is_test_seed_not_leaked(client: AsyncClient) -> None:
    """平台账号可用测试种子口令登录（确认种子走真实哈希校验），且响应不含口令。"""

    response = await client.post(
        "/api/v1/auth/login",
        json={"realm": "platform", "account": "ops-full", "password": TEST_PLATFORM_PASSWORD},
    )
    assert response.status_code == 200
    assert TEST_PLATFORM_PASSWORD not in response.text


async def test_bs_m1c_login_response_contains_tenant_and_roles(client: AsyncClient) -> None:
    """spec AUTH-01：登录响应含 tenant_id 与角色（前端据此渲染）。"""

    body = await _tenant_login(client)

    assert body["tenant_id"] is not None
    assert body["roles"] == []  # 学生暂无角色绑定，字段存在且为列表

    platform_body = await client.post(
        "/api/v1/auth/login",
        json={"realm": "platform", "account": "ops-full", "password": TEST_PLATFORM_PASSWORD},
    )
    assert platform_body.status_code == 200
    # 平台令牌没有 tenant_id：响应按 exclude_none 省略该字段
    assert platform_body.json().get("tenant_id") is None
    # roles 是角色码（不是权限码）；平台账号必须至少有一个角色
    assert platform_body.json()["roles"], platform_body.json()
    me = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {platform_body.json()['access_token']}"},
    )
    assert "platform.tenant.review" in me.json()["permissions"]


async def test_bs_m1c_oversized_password_is_rejected_not_500(client: AsyncClient) -> None:
    """MA2：超过 bcrypt 72 字节的口令必须 401/422，而不是 500。"""

    long_password = "a" * 100
    login = await client.post(
        "/api/v1/auth/login",
        json={
            "realm": "tenant",
            "school_code": SCHOOL_CODE,
            "login_id": "S-9001",
            "password": long_password,
        },
    )
    assert login.status_code == 401

    body = await _tenant_login(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    changed = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": TENANT_PASSWORD, "new_password": "汉" * 30},
        headers=headers,
    )
    assert changed.status_code == 422
    assert "字节" in changed.json()["detail"]


async def test_bs_m1c_refresh_is_single_winner_under_concurrency(client: AsyncClient) -> None:
    """并发刷新同一令牌：只有一个成功，另一个被判定为重用（家族撤销）。"""

    import asyncio

    body = await _tenant_login(client)
    refresh_token = str(body["refresh_token"])

    results = await asyncio.gather(
        client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token}),
        client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token}),
        return_exceptions=True,
    )
    statuses = sorted(
        item.status_code for item in results if not isinstance(item, BaseException)
    )
    assert statuses == [200, 401]

    # 终态断言：家族里只有一条被轮换（rotated_to_jti 非空），其余被重用检测撤销。
    # refresh_tokens 是租户级 RLS 表：必须用租户上下文读取（平台连接无权限）。
    from uuid import UUID as _UUID

    async with db_session(tenant_id=_UUID(str(body["tenant_id"]))) as session:
        rows = (
            await session.execute(
                text(
                    "select revoked_reason, rotated_to_jti from refresh_tokens "
                    "order by issued_at"
                )
            )
        ).mappings().all()
    reasons = sorted(str(row["revoked_reason"]) for row in rows)
    assert reasons == ["reuse_detected", "rotated"]
    assert sum(1 for row in rows if row["rotated_to_jti"] is not None) == 1


async def test_bs_m1c_platform_gate_blocks_business_endpoints(client: AsyncClient) -> None:
    """平台账号处于首登改密状态时，平台业务接口 403（门禁不区分 realm）。"""

    engine = make_test_engine("admin")
    try:
        await seed_platform_user(
            engine,
            account="ops-fresh",
            permissions=("platform.tenant.view",),
            must_change_password=True,
        )
    finally:
        await engine.dispose()

    login = await client.post(
        "/api/v1/auth/login",
        json={"realm": "platform", "account": "ops-fresh", "password": TEST_PLATFORM_PASSWORD},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    gated = await client.get("/api/v1/platform/tenants", headers=headers)
    assert gated.status_code == 403
    assert "修改密码" in gated.json()["detail"]

    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200


async def test_bs_m1c_tenant_token_cannot_reach_platform_routes(client: AsyncClient) -> None:
    """B1 回归：即使租户角色被映射了 platform.* 权限，平台路由也必须按 realm 拒绝。

    数据库守卫（b7d1e3f5a9c2）会先阻止该映射；本用例临时停用触发器以验证
    **应用层 realm 断言** 独立成立（纵深防御：数据库层之外的第二个门）。
    """

    # 先完成首登改密，解除门禁（否则会先命中"必须改密"的 403）
    first = await _tenant_login(client)
    first_headers = {"Authorization": f"Bearer {first['access_token']}"}
    changed = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": TENANT_PASSWORD, "new_password": NEW_PASSWORD},
        headers=first_headers,
    )
    assert changed.status_code == 200, changed.text

    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session, session.begin():
            await session.execute(
                text("alter table role_permissions disable trigger role_permissions_realm_guard")
            )
            try:
                tenant_id = await session.scalar(
                    text("select id from tenants where school_code = :code"),
                    {"code": SCHOOL_CODE},
                )
                role_id = await session.scalar(
                    text(
                        "insert into roles (tenant_id, code, name, is_system) "
                        "values (:tid, 'leak_role', '越界角色', false) returning id"
                    ),
                    {"tid": tenant_id},
                )
                user_id = await session.scalar(
                    text("select id from users where login_id = 'S-9001'")
                )
                await session.execute(
                    text(
                        "insert into user_roles (tenant_id, user_id, role_id) "
                        "values (:tid, :uid, :rid)"
                    ),
                    {"tid": tenant_id, "uid": user_id, "rid": role_id},
                )
                await session.execute(
                    text(
                        "insert into role_permissions (tenant_id, role_id, permission_id) "
                        "select :tid, :rid, id from permissions "
                        "where code = 'platform.tenant.view'"
                    ),
                    {"tid": tenant_id, "rid": role_id},
                )
            finally:
                await session.execute(
                    text("alter table role_permissions enable trigger role_permissions_realm_guard")
                )
    finally:
        # 清理植入的越界映射与角色（避免影响后续用例；触发器已恢复）
        cleanup_engine = make_test_engine("admin")
        try:
            cleanup_factory = make_session_factory(cleanup_engine)
            async with cleanup_factory() as session, session.begin():
                await session.execute(
                    text(
                        "delete from role_permissions where role_id in "
                        "(select id from roles where code = 'leak_role')"
                    )
                )
                await session.execute(
                    text(
                        "delete from user_roles where role_id in "
                        "(select id from roles where code = 'leak_role')"
                    )
                )
                await session.execute(text("delete from roles where code = 'leak_role'"))
        finally:
            await cleanup_engine.dispose()
        await engine.dispose()

    # 用新口令重新登录（角色映射后权限即时生效）
    body = await _tenant_login(client, password=NEW_PASSWORD)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    denied = await client.get("/api/v1/platform/tenants", headers=headers)
    assert denied.status_code == 403
    assert "不允许" in denied.json()["detail"]

    async with db_session(platform=True) as session:
        reasons: list[str] = list(
            (
                await session.execute(
                    text(
                        "select metadata_json::text from audit_logs "
                        "where action = 'auth.permission.denied'"
                    )
                )
            ).scalars().all()
        )
    assert any("realm_mismatch" in item for item in reasons)


async def test_bs_m1c_refresh_cookie_flow(client: AsyncClient) -> None:
    """M1-C-UI：刷新令牌走 HttpOnly Cookie——登录下发、仅凭 Cookie 可刷新、登出清除。"""

    login = await client.post(
        "/api/v1/auth/login",
        json={
            "realm": "tenant",
            "school_code": SCHOOL_CODE,
            "login_id": "S-9001",
            "password": TENANT_PASSWORD,
        },
    )
    assert login.status_code == 200, login.text
    set_cookie = login.headers.get("set-cookie", "")
    assert "campus_refresh_token=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert login.cookies.get("campus_refresh_token")

    # 不传 body、仅凭 Cookie 刷新（默认 httpx 客户端会在同一 cookie jar 内自动携带）
    refreshed = await client.post("/api/v1/auth/refresh", json={})
    assert refreshed.status_code == 200, refreshed.text
    assert "campus_refresh_token=" in refreshed.headers.get("set-cookie", "")

    access = refreshed.json()["access_token"]
    logout = await client.post(
        "/api/v1/auth/logout",
        json={},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert logout.status_code == 200
    # 清除 Cookie：Set-Cookie 中 max-age=0 或 expires 在过去
    cleared = logout.headers.get("set-cookie", "").lower()
    assert "campus_refresh_token=" in cleared and ("max-age=0" in cleared or "expires=" in cleared)


async def test_bs_m1c_refresh_without_token_is_rejected(client: AsyncClient) -> None:
    """既无 body 也无 Cookie 的刷新请求 → 401（不误用他人凭证）。"""

    client.cookies.clear()
    response = await client.post("/api/v1/auth/refresh", json={})
    assert response.status_code == 401


async def test_bs_m1c_change_password_refreshes_cookie(client: AsyncClient) -> None:
    """MA1：改密响应必须下发新的刷新 Cookie，且随后仅凭 Cookie 可刷新。"""

    body = await _tenant_login(client)
    access = str(body["access_token"])

    changed = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": TENANT_PASSWORD, "new_password": NEW_PASSWORD},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert changed.status_code == 200, changed.text
    assert "campus_refresh_token=" in changed.headers.get("set-cookie", "")

    refreshed = await client.post("/api/v1/auth/refresh", json={})
    assert refreshed.status_code == 200, refreshed.text


async def test_bs_m1c_logout_with_cookie_only(client: AsyncClient) -> None:
    """MA2：access 过期/缺失时，仅凭 Cookie 也必须能登出并撤销家族、清除 Cookie。"""

    await _tenant_login(client)
    assert client.cookies.get("campus_refresh_token")

    # 不带 Authorization（模拟 access 已过期）
    logout = await client.post("/api/v1/auth/logout", json={})
    assert logout.status_code == 200, logout.text
    cleared = logout.headers.get("set-cookie", "").lower()
    assert "campus_refresh_token=" in cleared
    assert "max-age=0" in cleared or "expires=" in cleared

    # 家族已撤销：Cookie 里的刷新令牌不再可用
    after = await client.post("/api/v1/auth/refresh", json={})
    assert after.status_code == 401

    actions = await _audit_actions()
    assert "auth.logout" in actions


async def test_bs_m1c_gate_403_carries_machine_readable_header(client: AsyncClient) -> None:
    """Minor 4：首登门禁的 403 带 X-Auth-Error 头，前端按机器码分流。"""

    body = await _tenant_login(client)
    gated = await client.get(
        "/api/v1/platform/tenants",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert gated.status_code == 403
    assert gated.headers.get("x-auth-error") == "PASSWORD_CHANGE_REQUIRED"
