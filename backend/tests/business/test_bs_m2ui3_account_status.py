"""业务场景 BS-M2UI3-H：权限账号停用 / 启用（TD-187e，HTTP 真实链路）。

Given 教务人员在读（已开号、绑了角色、持有会话），校方管理员持 `school.permission_account.manage`，
When 填写理由停用该账号，
Then 该账号旧 access 立即 401、刷新重放 401、重新登录被拒（401 账号不可用）、
    其活跃的刷新家族（未撤销且未过期）置 `admin_revoke`、Redis 会话标记消失、审计含状态/家族数/理由但不含登录名；
When 重新启用，
Then 可以重新登录、角色绑定与 MFA 状态原样保留，并留下启用审计。
同场验证：自停用 / 超管目标 / 学生目标 / 重复动作 / 理由过短 / 无权限操作者 / 跨租，
拒绝路径逐条留痕；另外钉住 Redis 删除失败的降级口径——停用仍 204、库内家族全撤销、
Redis 里留下一个孤儿标记，但 access/refresh/重新登录三条路仍被库内 `status` + `token_version`
硬门禁断开，且启用重登后设备列表里查不到那台设备（列表以库内 `revoked_at` 为准）。
"""

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth import revocation
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.main import create_app
from tests.support.auth import seed_tenant_user
from tests.support.db import clean_test_data, database_url, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.business, pytest.mark.integration]

OPERATOR_LOGIN = "status-operator"
OPERATOR_PASSWORD = "Status-Operator-Pass-1"
TEACHER_LOGIN = "status-teacher"
TEACHER_PASSWORD = "Status-Teacher-Pass-1"
SUPER_LOGIN = "status-super"
SUPER_PASSWORD = "Status-Super-Pass-1"
STUDENT_LOGIN = "status-student"
STUDENT_PASSWORD = "Status-Student-Pass-1"
REASON = "教务人员离职，账号即时停用（BS-M2UI3-H）"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
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
    """租户 + 内置角色 + 操作者/超管/教务/学生，并给教务绑一个非超管角色。"""

    engine = make_test_engine("admin")
    try:
        tenant_id, operator_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=OPERATOR_LOGIN,
            password=OPERATOR_PASSWORD,
            user_type="school_admin",
            permissions=("school.permission_account.manage",),
            scope_types=("school",),
        )
        _, super_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=SUPER_LOGIN,
            password=SUPER_PASSWORD,
            user_type="school_admin",
        )
        _, teacher_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=TEACHER_LOGIN,
            password=TEACHER_PASSWORD,
            user_type="teacher",
        )
        _, student_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=STUDENT_LOGIN,
            password=STUDENT_PASSWORD,
            user_type="student",
        )
        foreign_tenant_id, foreign_user_id = await seed_tenant_user(
            engine,
            school_code=f"{school_code}X",
            login_id=f"status-foreign-{school_code}",
            password=TEACHER_PASSWORD,
            user_type="teacher",
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text("select seed_builtin_roles(:tid)"), {"tid": str(tenant_id)}
            )
            await session.execute(
                text("select seed_builtin_roles(:tid)"), {"tid": str(foreign_tenant_id)}
            )
            await session.execute(
                text(
                    "insert into user_roles (tenant_id, user_id, role_id) "
                    "select :tid, :uid, r.id from roles r "
                    "where r.tenant_id = :tid and r.code = 'school_super_admin'"
                ),
                {"tid": str(tenant_id), "uid": str(super_id)},
            )
            await session.execute(
                text(
                    "insert into user_roles (tenant_id, user_id, role_id) "
                    "select :tid, :uid, r.id from roles r "
                    "where r.tenant_id = :tid and r.code = 'teacher_b_end'"
                ),
                {"tid": str(tenant_id), "uid": str(teacher_id)},
            )
        return {
            "tenant_id": tenant_id,
            "operator_id": operator_id,
            "super_id": super_id,
            "teacher_id": teacher_id,
            "student_id": student_id,
            "foreign_tenant_id": foreign_tenant_id,
            "foreign_user_id": foreign_user_id,
        }
    finally:
        await engine.dispose()


async def _login(client: AsyncClient, school_code: str, login_id: str, password: str) -> Any:
    return await client.post(
        "/api/v1/auth/login",
        json={
            "realm": "tenant",
            "school_code": school_code,
            "login_id": login_id,
            "password": password,
        },
    )


def _headers(response: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _user_row(tenant_id: UUID, user_id: UUID) -> dict[str, Any]:
    async with db_session(tenant_id=tenant_id) as session:
        row = (
            (
                await session.execute(
                    text(
                        "select status, token_version, mfa_enabled, failed_login_attempts, "
                        "locked_until from public.users where tenant_id = :tid and id = :uid"
                    ),
                    {"tid": tenant_id, "uid": user_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _refresh_rows(tenant_id: UUID, user_id: UUID) -> list[dict[str, Any]]:
    async with db_session(tenant_id=tenant_id) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select family_id, revoked_at, revoked_reason from public.refresh_tokens "
                        "where tenant_id = :tid and user_id = :uid"
                    ),
                    {"tid": tenant_id, "uid": user_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _role_codes(tenant_id: UUID, user_id: UUID) -> list[str]:
    async with db_session(tenant_id=tenant_id) as session:
        rows: list[Any] = list(
            (
                await session.execute(
                    text(
                        "select r.code from public.user_roles ur "
                        "join public.roles r on r.id = ur.role_id and r.tenant_id = ur.tenant_id "
                        "where ur.tenant_id = :tid and ur.user_id = :uid"
                    ),
                    {"tid": tenant_id, "uid": user_id},
                )
            )
            .scalars()
            .all()
        )
    return sorted(str(code) for code in rows)


async def _audit_rows(action: str, tenant_id: UUID) -> list[dict[str, Any]]:
    """按 `action` + 本用例租户取审计。

    必须带租户过滤：用例断言的是**精确列表**，别租户（=别的用例）残留的同名审计会把断言
    打成红且难以归因；平台连接能读全部审计，所以作用域要自己收紧。
    """

    async with db_session(platform=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select resource_id, actor_id::text as actor_id, result, metadata_json "
                        "from audit_logs "
                        "where action = :action and tenant_id = :tid order by sequence"
                    ),
                    {"action": action, "tid": tenant_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def test_deactivate_then_activate_round_trip(client: AsyncClient) -> None:
    """Given 教务在讀；When 停用；Then 会话/令牌/登录全断；When 启用；Then 恢复且绑定原样。"""

    school_code = f"SCH-STATUS{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    tenant_id = seeded["tenant_id"]
    teacher_id = seeded["teacher_id"]
    operator = _headers(await _login(client, school_code, OPERATOR_LOGIN, OPERATOR_PASSWORD))
    teacher_login = await _login(client, school_code, TEACHER_LOGIN, TEACHER_PASSWORD)
    assert teacher_login.status_code == 200, teacher_login.text
    teacher = _headers(teacher_login)
    teacher_refresh = teacher_login.cookies.get(get_settings().auth_cookie_name)
    assert teacher_refresh

    assert (await client.get("/api/v1/auth/me", headers=teacher)).status_code == 200
    bindings_before = await client.get(
        f"/api/v1/school/users/{teacher_id}/roles", headers=operator
    )
    assert bindings_before.status_code == 200, bindings_before.text
    assert [row["code"] for row in bindings_before.json()["roles"]] == ["teacher_b_end"]
    assert bindings_before.json()["status"] == "active"
    token_version_before = (await _user_row(tenant_id, teacher_id))["token_version"]

    # When：停用
    deactivated = await client.post(
        f"/api/v1/school/users/{teacher_id}/deactivate",
        json={"reason": REASON},
        headers=operator,
    )
    assert deactivated.status_code == 204, deactivated.text

    # Then：账号状态与令牌族
    after = await _user_row(tenant_id, teacher_id)
    assert after["status"] == "disabled"
    assert after["token_version"] == token_version_before + 1
    refresh_rows = await _refresh_rows(tenant_id, teacher_id)
    assert refresh_rows
    assert all(row["revoked_at"] is not None for row in refresh_rows)
    assert {row["revoked_reason"] for row in refresh_rows} == {"admin_revoke"}
    for row in refresh_rows:
        assert await revocation.session_exists(row["family_id"]) is False

    # Then：旧 access、旧 refresh、重新登录三条入口全部关闭
    assert (await client.get("/api/v1/auth/me", headers=teacher)).status_code == 401
    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": teacher_refresh}
    )
    assert replay.status_code == 401, replay.text
    relogin = await _login(client, school_code, TEACHER_LOGIN, TEACHER_PASSWORD)
    assert relogin.status_code == 401, relogin.text
    # 与密码错误同文案：登录入口不泄露"账号被停用"这一状态（反枚举）
    assert "不正确" in str(relogin.json()["detail"])

    # Then：绑定与权限并集不动，绑定快照反映 disabled
    bindings_after = await client.get(
        f"/api/v1/school/users/{teacher_id}/roles", headers=operator
    )
    assert bindings_after.status_code == 200
    assert bindings_after.json()["status"] == "disabled"
    assert [row["code"] for row in bindings_after.json()["roles"]] == ["teacher_b_end"]
    assert await _role_codes(tenant_id, teacher_id) == ["teacher_b_end"]

    # 审计：只有成功那一次，含状态/家族数/理由，不含登录名
    audits = await _audit_rows("school.user.deactivated", tenant_id)
    assert len(audits) == 1
    metadata = audits[0]["metadata_json"]
    assert set(metadata) == {"status_before", "sessions_revoked", "reason"}
    assert metadata["status_before"] == "active"
    assert metadata["sessions_revoked"] == len(refresh_rows)
    assert REASON in metadata["reason"]
    assert TEACHER_LOGIN not in str(metadata)
    # 谁做的：审计的 actor 是操作者本人，不是被停用的教师
    assert audits[0]["actor_id"] == str(seeded["operator_id"])
    assert audits[0]["resource_id"] == str(teacher_id)

    # When：重新启用
    activated = await client.post(
        f"/api/v1/school/users/{teacher_id}/activate", headers=operator
    )
    assert activated.status_code == 204, activated.text

    # Then：可重新登录，绑定与 MFA 状态原样，token_version 不再前进
    relogin_again = await _login(client, school_code, TEACHER_LOGIN, TEACHER_PASSWORD)
    assert relogin_again.status_code == 200, relogin_again.text
    me = await client.get("/api/v1/auth/me", headers=_headers(relogin_again))
    assert me.status_code == 200
    assert me.json()["mfa_enabled"] is False
    reactivated = await _user_row(tenant_id, teacher_id)
    assert reactivated["status"] == "active"
    assert reactivated["token_version"] == token_version_before + 1
    assert reactivated["failed_login_attempts"] == 0
    assert reactivated["locked_until"] is None
    assert await _role_codes(tenant_id, teacher_id) == ["teacher_b_end"]

    activated_audits = await _audit_rows("school.user.activated", tenant_id)
    assert len(activated_audits) == 1
    assert activated_audits[0]["metadata_json"] == {"status_before": "disabled"}
    assert activated_audits[0]["resource_id"] == str(teacher_id)
    assert activated_audits[0]["actor_id"] == str(seeded["operator_id"])


async def test_redis_cleanup_failure_still_revokes_access_via_database_gate(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given Redis 删除失败（标记删不掉、键还在）；When 停用；Then 仍 204、库内硬门禁断开全部令牌、
    启用重登后设备列表也不多出一条。

    故意打桩**底层客户端**而不是 `delete_sessions` 本身：后者内部吞 `RedisError` 那段正是
    TD-102 的降级代码，直接替换掉它就只能证明"调用方不炸"，证不到"清理失败不会把停用
    变成 500"。
    """

    school_code = f"SCH-STATUS{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    tenant_id = seeded["tenant_id"]
    teacher_id = seeded["teacher_id"]
    operator = _headers(await _login(client, school_code, OPERATOR_LOGIN, OPERATOR_PASSWORD))
    teacher_login = await _login(client, school_code, TEACHER_LOGIN, TEACHER_PASSWORD)
    assert teacher_login.status_code == 200, teacher_login.text
    teacher = _headers(teacher_login)
    teacher_refresh = teacher_login.cookies.get(get_settings().auth_cookie_name)
    assert teacher_refresh

    class _DeleteFails:
        """只让 `delete` 抛错，其余方法透传真实客户端——于是键删不掉，但读得到。"""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def delete(self, *keys: str) -> int:
            raise RedisError("模拟 Redis 删除失败")

    # 先取真实客户端再打桩：在 lambda 里调用 `revocation._redis()` 会递归到自己
    lossy = _DeleteFails(revocation._redis())
    monkeypatch.setattr(revocation, "_redis", lambda: lossy)

    deactivated = await client.post(
        f"/api/v1/school/users/{teacher_id}/deactivate",
        json={"reason": REASON},
        headers=operator,
    )
    assert deactivated.status_code == 204, deactivated.text

    # 清理丢失的真实痕迹：Redis 会话标记仍在（孤儿键，由 TTL 回收）
    rows = await _refresh_rows(tenant_id, teacher_id)
    assert rows and all(row["revoked_at"] is not None for row in rows)
    for row in rows:
        assert await revocation.session_exists(row["family_id"]) is True

    # 安全结果不受影响：状态与 token_version 是硬门禁
    assert (await _user_row(tenant_id, teacher_id))["status"] == "disabled"
    assert (await client.get("/api/v1/auth/me", headers=teacher)).status_code == 401
    replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": teacher_refresh})
    assert replay.status_code == 401, replay.text
    assert (await _login(client, school_code, TEACHER_LOGIN, TEACHER_PASSWORD)).status_code == 401

    # 孤儿标记不改变用户可见结果：设备列表以库内 `revoked_at is null` 为准 ∩ Redis 标记，
    # 已撤销的家族不会因为标记残留而多出一条
    revoked_families = {str(row["family_id"]) for row in rows}
    assert revoked_families
    assert (
        await client.post(f"/api/v1/school/users/{teacher_id}/activate", headers=operator)
    ).status_code == 204
    relogin = await _login(client, school_code, TEACHER_LOGIN, TEACHER_PASSWORD)
    assert relogin.status_code == 200, relogin.text
    listed = await client.get("/api/v1/auth/sessions", headers=_headers(relogin))
    assert listed.status_code == 200, listed.text
    visible = {str(item["session_id"]) for item in listed.json()["sessions"]}
    assert not revoked_families & visible, visible


async def test_denied_status_changes_are_audited_and_leave_the_target_untouched(
    client: AsyncClient,
) -> None:
    """Given 七类非法请求；When 逐条发起；Then 状态码与判据码固定、目标不变、拒绝审计逐条留痕。"""

    school_code = f"SCH-STATUS{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    tenant_id = seeded["tenant_id"]
    teacher_id = seeded["teacher_id"]
    operator_login = await _login(client, school_code, OPERATOR_LOGIN, OPERATOR_PASSWORD)
    operator = _headers(operator_login)
    student = _headers(await _login(client, school_code, STUDENT_LOGIN, STUDENT_PASSWORD))
    teacher_before = await _user_row(tenant_id, teacher_id)
    super_before = await _user_row(tenant_id, seeded["super_id"])

    # 无权限操作者：鉴权依赖挡在路由层（不进业务审计）
    denied_by_permission = await client.post(
        f"/api/v1/school/users/{teacher_id}/deactivate",
        json={"reason": REASON},
        headers=student,
    )
    assert denied_by_permission.status_code == 403, denied_by_permission.text

    # 自己：409（用操作者自己的 id）
    self_target = await client.post(
        f"/api/v1/school/users/{seeded['operator_id']}/deactivate",
        json={"reason": REASON},
        headers=operator,
    )
    assert self_target.status_code == 409, self_target.text
    assert self_target.json()["detail"]["code"] == "ACCOUNT_SELF_DEACTIVATE"

    # 超管：停用与启用都 403
    super_deactivate = await client.post(
        f"/api/v1/school/users/{seeded['super_id']}/deactivate",
        json={"reason": REASON},
        headers=operator,
    )
    assert super_deactivate.status_code == 403, super_deactivate.text
    assert super_deactivate.json()["detail"]["code"] == "ACCOUNT_SUPER_ADMIN_FORBIDDEN"
    super_activate = await client.post(
        f"/api/v1/school/users/{seeded['super_id']}/activate", headers=operator
    )
    assert super_activate.status_code == 403
    assert super_activate.json()["detail"]["code"] == "ACCOUNT_SUPER_ADMIN_FORBIDDEN"

    # 跨租：同形 404
    foreign = await client.post(
        f"/api/v1/school/users/{seeded['foreign_user_id']}/deactivate",
        json={"reason": REASON},
        headers=operator,
    )
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["detail"]["code"] == "USER_NOT_FOUND"

    # 学生：C 端账号不在权限账号射程内，两个方向都 422
    student_deactivate = await client.post(
        f"/api/v1/school/users/{seeded['student_id']}/deactivate",
        json={"reason": REASON},
        headers=operator,
    )
    assert student_deactivate.status_code == 422, student_deactivate.text
    assert student_deactivate.json()["detail"]["code"] == "ACCOUNT_TARGET_NOT_ALLOWED"
    student_activate = await client.post(
        f"/api/v1/school/users/{seeded['student_id']}/activate", headers=operator
    )
    assert student_activate.status_code == 422
    assert student_activate.json()["detail"]["code"] == "ACCOUNT_TARGET_NOT_ALLOWED"

    # 理由缺失/过短：422 业务码（不是 FastAPI 数组形）
    for payload in ({}, {"reason": ""}, {"reason": "  短  "}):
        short = await client.post(
            f"/api/v1/school/users/{teacher_id}/deactivate",
            json=payload,
            headers=operator,
        )
        assert short.status_code == 422, short.text
        assert short.json()["detail"]["code"] == "DEACTIVATE_REASON_REQUIRED"
    assert await _user_row(tenant_id, teacher_id) == teacher_before

    # 重复动作：停用两次 409，启用活跃账号 409
    assert (
        await client.post(
            f"/api/v1/school/users/{teacher_id}/deactivate",
            json={"reason": REASON},
            headers=operator,
        )
    ).status_code == 204
    again = await client.post(
        f"/api/v1/school/users/{teacher_id}/deactivate",
        json={"reason": REASON},
        headers=operator,
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "ACCOUNT_ALREADY_DISABLED"
    assert (
        await client.post(
            f"/api/v1/school/users/{teacher_id}/activate", headers=operator
        )
    ).status_code == 204
    active_again = await client.post(
        f"/api/v1/school/users/{teacher_id}/activate", headers=operator
    )
    assert active_again.status_code == 409
    assert active_again.json()["detail"]["code"] == "ACCOUNT_ALREADY_ACTIVE"

    # 目标状态未被拒绝路径改动
    assert (await _user_row(tenant_id, seeded["super_id"])) == super_before
    assert (await _user_row(tenant_id, seeded["student_id"]))["status"] == "active"
    assert (
        await _user_row(seeded["foreign_tenant_id"], seeded["foreign_user_id"])
    )["status"] == "active"

    denied = await _audit_rows("school.user.status_denied", tenant_id)
    assert [row["metadata_json"]["reason"] for row in denied] == [
        "ACCOUNT_SELF_DEACTIVATE",
        "ACCOUNT_SUPER_ADMIN_FORBIDDEN",
        "ACCOUNT_SUPER_ADMIN_FORBIDDEN",
        "USER_NOT_FOUND",
        "ACCOUNT_TARGET_NOT_ALLOWED",
        "ACCOUNT_TARGET_NOT_ALLOWED",
        "ACCOUNT_ALREADY_DISABLED",
        "ACCOUNT_ALREADY_ACTIVE",
    ], denied
    assert [row["metadata_json"]["operation"] for row in denied] == [
        "deactivate",
        "deactivate",
        "activate",
        "deactivate",
        "deactivate",
        "activate",
        "deactivate",
        "activate",
    ]
    assert {row["result"] for row in denied} == {"denied"}
    assert denied[0]["resource_id"] == str(seeded["operator_id"])
    assert denied[1]["resource_id"] == str(seeded["super_id"])
    assert denied[3]["resource_id"] == str(seeded["foreign_user_id"])
    assert denied[4]["resource_id"] == str(seeded["student_id"])
    assert denied[5]["resource_id"] == str(seeded["student_id"])
    for row in denied:
        assert set(row["metadata_json"]) == {"operation", "reason"}
        assert TEACHER_LOGIN not in str(row["metadata_json"])
        assert OPERATOR_LOGIN not in str(row["metadata_json"])
        # 每一条拒绝都记得下"是谁发起的"（越权探测的巡检口径）
        assert row["actor_id"] == str(seeded["operator_id"])
