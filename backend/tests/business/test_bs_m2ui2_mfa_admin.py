"""业务场景 BS-M2UI2-B：租户侧 MFA 管理员重置（TD-126，HTTP 真实链路）。

Given 教师丢了手机、恢复码也用完，校方管理员持 `school.personnel.mfa.reset`，
When 按登录名精确找到目标并填写理由重置，
Then MFA 绑定与恢复码被清空、全部刷新家族撤销、旧 access 立即失效（token_version 递增）、
    审计含理由但不含 secret/登录名；同时验证不能重置自己/超管、跨租与越权均被拒。
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
from app.security.encryption import decode_kek, encrypt_secret, mfa_aad
from app.security.totp import generate_totp_secret
from tests.support.auth import seed_tenant_user
from tests.support.db import clean_test_data, database_url, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.business, pytest.mark.integration]

OPERATOR_LOGIN = "mfa-operator"
OPERATOR_PASSWORD = "Mfa-Operator-Pass-1"
TEACHER_LOGIN = "mfa-teacher"
TEACHER_PASSWORD = "Mfa-Teacher-Pass-1"
SUPER_LOGIN = "mfa-super"
SUPER_PASSWORD = "Mfa-Super-Pass-1"
PLAIN_LOGIN = "mfa-plain"
PLAIN_PASSWORD = "Mfa-Plain-Pass-1"


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
    """建租户、内置角色与四类账号：操作者、超管目标、待重置教师、普通教师。"""

    engine = make_test_engine("admin")
    try:
        tenant_id, operator_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=OPERATOR_LOGIN,
            password=OPERATOR_PASSWORD,
            user_type="school_admin",
            permissions=("school.personnel.mfa.reset",),
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
        _, plain_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=PLAIN_LOGIN,
            password=PLAIN_PASSWORD,
            user_type="teacher",
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
        return {
            "tenant_id": tenant_id,
            "operator_id": operator_id,
            "super_id": super_id,
            "teacher_id": teacher_id,
            "plain_id": plain_id,
        }
    finally:
        await engine.dispose()


async def _login(client: AsyncClient, school_code: str, login_id: str, password: str) -> Any:
    response = await client.post(
        "/api/v1/auth/login",
        json={"realm": "tenant", "school_code": school_code, "login_id": login_id,
              "password": password},
    )
    assert response.status_code == 200, response.text
    return response


def _headers(response: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _enable_mfa(tenant_id: UUID, user_id: UUID) -> None:
    """直接写加密 secret 并启用（绑定流程由 M1-D2 测试覆盖，这里只要目标状态）。"""

    encrypted = encrypt_secret(
        generate_totp_secret(),
        kek=decode_kek(get_settings().encryption_kek),
        aad=mfa_aad("tenant", user_id),
    )
    engine = make_test_engine("admin")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "update users set mfa_enabled = true, mfa_secret_enc = :enc, "
                    "failed_login_attempts = 3, locked_until = now() + interval '10 minutes' "
                    "where tenant_id = :tid and id = :uid"
                ),
                {"enc": encrypted, "tid": str(tenant_id), "uid": str(user_id)},
            )
    finally:
        await engine.dispose()


async def _seed_recovery_codes(tenant_id: UUID, user_id: UUID) -> None:
    engine = make_test_engine("admin")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "insert into mfa_recovery_codes (tenant_id, user_id, code_hash) "
                    "values (:tid, :uid, gen_random_uuid()::text)"
                ),
                {"tid": str(tenant_id), "uid": str(user_id)},
            )
            await connection.execute(
                text(
                    "insert into mfa_recovery_codes (tenant_id, user_id, code_hash) "
                    "values (:tid, :uid, gen_random_uuid()::text)"
                ),
                {"tid": str(tenant_id), "uid": str(user_id)},
            )
    finally:
        await engine.dispose()


async def _user_state(tenant_id: UUID, user_id: UUID) -> dict[str, Any]:
    async with db_session(tenant_id=tenant_id) as session:
        row = (
            (
                await session.execute(
                    text(
                        "select mfa_enabled, mfa_secret_enc, mfa_last_used_step, "
                        "failed_login_attempts, locked_until, token_version "
                        "from users where tenant_id = :tid and id = :uid"
                    ),
                    {"tid": tenant_id, "uid": user_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _refresh_state(tenant_id: UUID, user_id: UUID) -> list[dict[str, Any]]:
    async with db_session(tenant_id=tenant_id) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select revoked_at, revoked_reason from refresh_tokens "
                        "where tenant_id = :tid and user_id = :uid"
                    ),
                    {"tid": tenant_id, "uid": user_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _recovery_count(tenant_id: UUID, user_id: UUID) -> int:
    async with db_session(tenant_id=tenant_id) as session:
        value = await session.scalar(
            text(
                "select count(*) from mfa_recovery_codes "
                "where tenant_id = :tid and user_id = :uid"
            ),
            {"tid": tenant_id, "uid": user_id},
        )
    return int(value or 0)


async def _audit_rows(action: str, tenant_id: UUID) -> list[dict[str, Any]]:
    """按 `action` + **本用例租户**取审计（TD-228）。

    本文件的跨租用例（重置别租用户按 `USER_NOT_FOUND`）由**本租管理员**发起，拒绝审计落的就是本租，
    这条谓词不会把它们挡掉，也不该挡。它挡的是以后可能出现的两类污染：①在同一条用例里让别租管理员
    当操作者，那时同名动作会多出别租的行；②失败路径上未 `cancel()` 的并发任务在下一条用例的
    `TRUNCATE` 之后才提交。这两类都会把"顺序与条数都算准"的精确断言打成随机红。
    """

    async with db_session(platform=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select resource_id, result, metadata_json from audit_logs "
                        "where action = :action and tenant_id = :tid order by sequence"
                    ),
                    {"action": action, "tid": str(tenant_id)},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def test_lookup_is_precise_audited_and_permissioned(client: AsyncClient) -> None:
    """Given 操作者只有 mfa.reset；When 按登录名查询；Then 只回最小字段、查询留审计、无权限者 403。"""

    school_code = f"SCH-MFA{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    operator = _headers(
        await _login(client, school_code, OPERATOR_LOGIN, OPERATOR_PASSWORD)
    )
    teacher = _headers(await _login(client, school_code, TEACHER_LOGIN, TEACHER_PASSWORD))

    # 教师没有任何学校后台权限：查询接口 403（前端隐藏不算边界）
    denied = await client.get(
        "/api/v1/school/users/lookup", params={"login_id": PLAIN_LOGIN}, headers=teacher
    )
    assert denied.status_code == 403, denied.text

    found = await client.get(
        "/api/v1/school/users/lookup", params={"login_id": TEACHER_LOGIN}, headers=operator
    )
    assert found.status_code == 200, found.text
    body = found.json()
    assert set(body) == {"id", "display_name", "user_type", "status", "mfa_enabled", "roles"}
    assert body["id"] == str(seeded["teacher_id"])
    assert body["user_type"] == "teacher"
    assert body["mfa_enabled"] is False
    assert body["roles"] == []

    # 违规超管在 UI 上要有迹象：角色列表要能看到 school_super_admin
    super_body = await client.get(
        "/api/v1/school/users/lookup", params={"login_id": SUPER_LOGIN}, headers=operator
    )
    assert super_body.status_code == 200
    assert super_body.json()["roles"] == ["school_super_admin"]

    missing = await client.get(
        "/api/v1/school/users/lookup", params={"login_id": "no-such-user"}, headers=operator
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["code"] == "USER_NOT_FOUND"

    rows = await _audit_rows("school.user.lookup", seeded["tenant_id"])
    assert [row["metadata_json"]["found"] for row in rows] == [True, True, False]
    # 未命中是独立事务写的 denied 行：外层 404 回滚不会带走它，枚举探测才留下痕迹
    assert [row["result"] for row in rows] == ["success", "success", "denied"]
    for row in rows:
        assert set(row["metadata_json"]) == {"found"}
        assert "no-such-user" not in str(row["metadata_json"])
        assert TEACHER_LOGIN not in str(row["metadata_json"])
    assert rows[-1]["resource_id"] is None


async def test_mfa_reset_clears_binding_revokes_sessions_and_guards_targets(
    client: AsyncClient,
) -> None:
    """Given 教师已绑定 MFA 且有会话；When 操作者重置；Then 清绑定/锁定、撤会话、旧令牌失效、审计脱敏。"""

    school_code = f"SCH-MFA{uuid4().hex[:6].upper()}"
    seeded = await _seed_school(school_code)
    tenant_id = seeded["tenant_id"]
    operator = _headers(
        await _login(client, school_code, OPERATOR_LOGIN, OPERATOR_PASSWORD)
    )
    teacher_login = await _login(client, school_code, TEACHER_LOGIN, TEACHER_PASSWORD)
    teacher = _headers(teacher_login)
    teacher_refresh = teacher_login.cookies.get(get_settings().auth_cookie_name)
    assert teacher_refresh

    # 绑定 MFA、留恢复码、制造一次失败锁定：重置应把三者一起清掉
    await _enable_mfa(tenant_id, seeded["teacher_id"])
    await _seed_recovery_codes(tenant_id, seeded["teacher_id"])
    await _enable_mfa(tenant_id, seeded["super_id"])
    before = await _user_state(tenant_id, seeded["teacher_id"])
    assert before["mfa_enabled"] is True

    # 自我保护：不能重置自己（即使自己没开 MFA）
    self_reset = await client.post(
        f"/api/v1/school/users/{seeded['operator_id']}/mfa/reset",
        json={"reason": "验证管理员不能重置自己的 MFA 绑定"},
        headers=operator,
    )
    assert self_reset.status_code == 409, self_reset.text
    assert self_reset.json()["detail"]["code"] == "MFA_SELF_RESET"

    # 超管兜底走平台：租户侧禁止
    super_reset = await client.post(
        f"/api/v1/school/users/{seeded['super_id']}/mfa/reset",
        json={"reason": "验证校方超管的 MFA 必须走平台兜底"},
        headers=operator,
    )
    assert super_reset.status_code == 403, super_reset.text
    assert super_reset.json()["detail"]["code"] == "MFA_SUPER_ADMIN_FORBIDDEN"

    # 未启用 MFA 的目标：409（不能把重置当"提前清空"用）
    plain_reset = await client.post(
        f"/api/v1/school/users/{seeded['plain_id']}/mfa/reset",
        json={"reason": "验证未启用 MFA 的目标返回冲突"},
        headers=operator,
    )
    assert plain_reset.status_code == 409, plain_reset.text
    assert plain_reset.json()["detail"]["code"] == "MFA_NOT_ENABLED"

    # 跨租：不区分「不存在」与「不是你的」
    foreign_code = f"SCH-MFA{uuid4().hex[:6].upper()}"
    engine = make_test_engine("admin")
    try:
        _, foreign_user_id = await seed_tenant_user(
            engine,
            school_code=foreign_code,
            login_id="mfa-foreign",
            password="Mfa-Foreign-Pass-1",
        )
    finally:
        await engine.dispose()
    foreign = await client.post(
        f"/api/v1/school/users/{foreign_user_id}/mfa/reset",
        json={"reason": "验证跨租用户一律按不存在处理"},
        headers=operator,
    )
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["detail"]["code"] == "USER_NOT_FOUND"

    # 理由不足：422，且不产生任何改动
    short = await client.post(
        f"/api/v1/school/users/{seeded['teacher_id']}/mfa/reset",
        json={"reason": "换手机"},
        headers=operator,
    )
    assert short.status_code == 422, short.text
    assert short.json()["detail"]["code"] == "RESET_REASON_REQUIRED"
    assert (await _user_state(tenant_id, seeded["teacher_id"]))["mfa_enabled"] is True

    # When：真正重置
    reason = "教师更换手机且恢复码用尽（工单 TD-126 场景）"
    reset = await client.post(
        f"/api/v1/school/users/{seeded['teacher_id']}/mfa/reset",
        json={"reason": reason},
        headers=operator,
    )
    assert reset.status_code == 204, reset.text

    # Then：MFA/锁定清空、版本递增、恢复码与刷新家族撤销
    after = await _user_state(tenant_id, seeded["teacher_id"])
    assert after["mfa_enabled"] is False
    assert after["mfa_secret_enc"] is None
    assert after["mfa_last_used_step"] is None
    assert after["failed_login_attempts"] == 0
    assert after["locked_until"] is None
    assert after["token_version"] == before["token_version"] + 1
    assert await _recovery_count(tenant_id, seeded["teacher_id"]) == 0
    refresh_rows = await _refresh_state(tenant_id, seeded["teacher_id"])
    assert refresh_rows
    assert all(row["revoked_at"] is not None for row in refresh_rows)
    assert {row["revoked_reason"] for row in refresh_rows} == {"admin_revoke"}

    # 旧 access 与旧 refresh 都不能再用（token_version + 家族撤销）
    assert (await client.get("/api/v1/auth/me", headers=teacher)).status_code == 401
    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": teacher_refresh}
    )
    assert replay.status_code == 401, replay.text

    # 密码未变：可重新密码登录，且会话里已无 MFA
    relogin = await _login(client, school_code, TEACHER_LOGIN, TEACHER_PASSWORD)
    assert (await client.get("/api/v1/auth/me", headers=_headers(relogin))).json()[
        "mfa_enabled"
    ] is False

    # 审计：只有成功那一次，含理由与撤销家族数，不含 secret/恢复码/登录名
    rows = await _audit_rows("school.user.mfa_reset", tenant_id)
    assert len(rows) == 1
    metadata = rows[0]["metadata_json"]
    assert set(metadata) == {"sessions_revoked", "reason"}
    assert metadata["sessions_revoked"] == 1
    assert reason in metadata["reason"]
    assert "secret" not in str(metadata).lower()
    assert TEACHER_LOGIN not in str(metadata)

    # 幂等边界：已清空后再重置 → 409（不产生第二条成功审计）
    again = await client.post(
        f"/api/v1/school/users/{seeded['teacher_id']}/mfa/reset",
        json={"reason": reason},
        headers=operator,
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "MFA_NOT_ENABLED"
    assert len(await _audit_rows("school.user.mfa_reset", tenant_id)) == 1

    # 被挡的重试同样留痕：外层事务回滚会带走同事务的审计，这里验独立事务写成功。
    # 顺序即用例顺序：自己 → 超管 → 未启用 → 跨租（按不存在处理）→ 再次未启用。
    # 判据码统一 SCREAMING_SNAKE：M2 的审计巡检会按 reason 分组统计，大小写混用会分成两组。
    denied = await _audit_rows("school.user.mfa_reset_denied", tenant_id)
    assert [row["metadata_json"]["reason"] for row in denied] == [
        "MFA_SELF_RESET",
        "MFA_SUPER_ADMIN_FORBIDDEN",
        "MFA_NOT_ENABLED",
        "USER_NOT_FOUND",
        "MFA_NOT_ENABLED",
    ], denied
    assert {row["result"] for row in denied} == {"denied"}
    assert denied[1]["resource_id"] == str(seeded["super_id"])
    assert denied[3]["resource_id"] == str(foreign_user_id)
    for row in denied:
        assert set(row["metadata_json"]) == {"reason"}
        assert TEACHER_LOGIN not in str(row["metadata_json"])
