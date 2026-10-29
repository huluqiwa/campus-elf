"""业务场景 BS-48：登录设备限额、设备列表、单台下线、退出其它设备（HTTP 全链路）。

断言业务结果而非仅状态码：`refresh_tokens` 家族状态、Redis 会话标记、审计动作。
"""

import json
import os
import shutil
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError
from sqlalchemy import text

from app.auth import revocation
from app.auth.passwords import hash_password
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.main import create_app
from tests.support.auth import TEST_PLATFORM_PASSWORD, seed_platform_user
from tests.support.db import (
    clean_test_data,
    database_url,
    make_session_factory,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.business, pytest.mark.integration]

TENANT_PASSWORD = "Student-Session-Pass-1"
NEW_PASSWORD = "Student-New-Session-2"
SCHOOL_CODE = "SCH-BSSESS01"
SECOND_SCHOOL_CODE = "SCH-BSSESS02"

CHROME_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
SAFARI_IOS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
FIREFOX_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) "
    "Gecko/20100101 Firefox/120.0"
)
CHROME_ANDROID = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)


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
    """指向测试库/Redis 的应用客户端，并准备租户用户与平台账号。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
        factory = make_session_factory(admin_engine)
        async with factory() as session, session.begin():
            tenant_id = await session.scalar(
                text(
                    "insert into tenants (school_code, name, status, activated_at) "
                    "values (:code, '业务会话中学', 'active', now()) returning id"
                ),
                {"code": SCHOOL_CODE},
            )
            await session.execute(
                text(
                    "insert into users (tenant_id, user_type, login_id, display_name, "
                    "password_hash, status, must_change_password, mfa_enabled) "
                    "values (:tenant_id, 'student', 'S-9101', '会话学生', :hash, 'active', "
                    "false, false)"
                ),
                {"tenant_id": tenant_id, "hash": hash_password(TENANT_PASSWORD)},
            )
            await session.execute(
                text(
                    "insert into users (tenant_id, user_type, login_id, display_name, "
                    "password_hash, status, must_change_password, mfa_enabled) "
                    "values (:tenant_id, 'student', 'S-9102', '首登学生', :hash, 'active', "
                    "true, false)"
                ),
                {"tenant_id": tenant_id, "hash": hash_password(TENANT_PASSWORD)},
            )
            await session.execute(
                text(
                    "insert into users (tenant_id, user_type, login_id, display_name, "
                    "password_hash, status, must_change_password, mfa_enabled) "
                    "values (:tenant_id, 'student', 'S-9103', '另一学生', :hash, 'active', "
                    "false, false)"
                ),
                {"tenant_id": tenant_id, "hash": hash_password(TENANT_PASSWORD)},
            )
            second_tenant_id = await session.scalar(
                text(
                    "insert into tenants (school_code, name, status, activated_at) "
                    "values (:code, '业务会话二中', 'active', now()) returning id"
                ),
                {"code": SECOND_SCHOOL_CODE},
            )
            await session.execute(
                text(
                    "insert into users (tenant_id, user_type, login_id, display_name, "
                    "password_hash, status, must_change_password, mfa_enabled) "
                    "values (:tenant_id, 'student', 'S-9201', '他校学生', :hash, 'active', "
                    "false, false)"
                ),
                {"tenant_id": second_tenant_id, "hash": hash_password(TENANT_PASSWORD)},
            )
        await seed_platform_user(admin_engine, account="ops-sessions")
    finally:
        await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    monkeypatch.setenv("MAX_SESSIONS_PER_USER_TENANT", "3")
    monkeypatch.setenv("MAX_SESSIONS_PER_USER_PLATFORM", "2")
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


async def _login_device(
    client: AsyncClient,
    user_agent: str,
    *,
    login_id: str = "S-9101",
    school_code: str = SCHOOL_CODE,
) -> dict[str, object]:
    """C 端学生从指定设备登录，返回响应体。"""

    response = await client.post(
        "/api/v1/auth/login",
        json={
            "realm": "tenant",
            "school_code": school_code,
            "login_id": login_id,
            "password": TENANT_PASSWORD,
        },
        headers={"user-agent": user_agent},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _headers(body: dict[str, object]) -> dict[str, str]:
    """构造 Bearer 头。"""

    return {"Authorization": f"Bearer {body['access_token']}"}


async def _list_sessions(client: AsyncClient, body: dict[str, object]) -> list[dict[str, object]]:
    """读取当前账号的设备列表。"""

    response = await client.get("/api/v1/auth/sessions", headers=_headers(body))
    assert response.status_code == 200, response.text
    return list(response.json()["sessions"])


async def _audit_rows(action: str, tenant_id: UUID) -> list[str]:
    """读取指定租户下某审计动作的 metadata JSON 文本（TD-228：谓词必须带租户）。

    本助手只回文本、不回 `tenant_id` 列，所以无法在 Python 里收窄；而下面这些断言用的是
    精确条数或"必须为空"。每条用例开头会 `TRUNCATE audit_logs`，所以残留不会一直躺着——
    真实路径是同进程里更早的**并发用例**（本文件自己不含 `create_task`，例如
    `tests/integration/test_auth_sessions.py`）没被 `cancel()` 的任务，在下一条用例的 TRUNCATE
    之后才提交，那一行就落进别人的时间窗，正好把这些计数打歪（且报错位置离原因很远）。
    平台 realm 的审计（`tenant_id` 为空）走 `_platform_audit_rows`。
    """

    async with db_session(platform=True) as session:
        return list(
            (
                await session.execute(
                    text(
                        "select metadata_json::text from audit_logs "
                        "where action = :action and tenant_id = :tid order by created_at"
                    ),
                    {"action": action, "tid": str(tenant_id)},
                )
            )
            .scalars()
            .all()
        )


async def _audit_rows_all(action: str) -> list[str]:
    """读取某审计动作的 metadata JSON 文本（**全表口径**，不按租户收窄）。

    「这条动作一次都不该发生」类断言必须用它：按租户收窄会漏掉**写错租户**的那一行，
    而那恰恰是要抓的 bug。精确条数类断言才用带租户的 `_audit_rows`（TD-228）。
    """

    async with db_session(platform=True) as session:
        return list(
            (
                await session.execute(
                    text(
                        "select metadata_json::text from audit_logs "
                        "where action = :action order by created_at"
                    ),
                    {"action": action},
                )
            )
            .scalars()
            .all()
        )


async def _platform_audit_rows(action: str) -> list[str]:
    """读取平台 realm 某审计动作的 metadata JSON 文本（`tenant_id` 为空那一支）。

    `app/auth/sessions.py` 写审计时是 `tenant_id if realm == "tenant" else None`，所以平台
    动作只能按"租户为空"这一条来数；用 `_audit_rows` 会一条也数不到。
    """

    async with db_session(platform=True) as session:
        return list(
            (
                await session.execute(
                    text(
                        "select metadata_json::text from audit_logs "
                        "where action = :action and tenant_id is null order by created_at"
                    ),
                    {"action": action},
                )
            )
            .scalars()
            .all()
        )


async def _audit_rows_with_tenant(
    action: str, *tenant_ids: UUID
) -> list[tuple[UUID | None, UUID | None, str, dict[str, object]]]:
    """读取审计动作的 tenant/actor 与 metadata，只收给定租户白名单内的行（TD-228）。

    这两条用例本身要看见"操作者一行 + 主体一行"，所以不能按单个租户过滤；但环境里其它
    租户的同名 `auth.logout` 也不该进这条计数——`tenant_ids` 就是本用例明确涉及的租户。
    """

    assert 1 <= len(tenant_ids) <= 2, "该助手只支持一个租户或跨租用例的两个租户"
    if len(tenant_ids) == 1:
        statement = text(
            "select tenant_id, actor_id, actor_realm, metadata_json::text "
            "from audit_logs where action = :action and tenant_id = :tid_0 order by created_at"
        )
    else:
        statement = text(
            "select tenant_id, actor_id, actor_realm, metadata_json::text "
            "from audit_logs where action = :action "
            "and (tenant_id = :tid_0 or tenant_id = :tid_1) order by created_at"
        )
    params: dict[str, object] = {"action": action}
    for index, tenant_id in enumerate(tenant_ids):
        params[f"tid_{index}"] = str(tenant_id)
    async with db_session(platform=True) as session:
        rows = (await session.execute(statement, params)).all()
    return [(row[0], row[1], str(row[2]), json.loads(row[3])) for row in rows]


async def _user_id_for(tenant_id: UUID, login_id: str) -> UUID:
    """按租户与登录名读取用户 id。"""

    async with db_session(tenant_id=tenant_id) as session:
        value = await session.scalar(
            text("select id from users where tenant_id = :tid and login_id = :login"),
            {"tid": tenant_id, "login": login_id},
        )
    assert value is not None
    return UUID(str(value))


async def _active_family_count(tenant_id: UUID, user_id: UUID) -> int:
    """租户用户的活跃 family 数。"""

    async with db_session(tenant_id=tenant_id) as session:
        return int(
            await session.scalar(
                text(
                    "select count(distinct family_id) from refresh_tokens "
                    "where user_id = :uid and revoked_at is null and expires_at > now()"
                ),
                {"uid": user_id},
            )
            or 0
        )


async def _family_reason(tenant_id: UUID, session_id: UUID) -> str | None:
    """租户 family 最新一行的撤销原因。"""

    async with db_session(tenant_id=tenant_id) as session:
        value = await session.scalar(
            text(
                "select revoked_reason from refresh_tokens "
                "where family_id = :fid order by issued_at desc limit 1"
            ),
            {"fid": session_id},
        )
    return str(value) if value is not None else None


async def _age_session(
    tenant_id: UUID, session_id: UUID, interval: timedelta
) -> None:
    """把 family 的最近使用时间后移，制造确定的"最久未用"顺序。"""

    async with db_session(tenant_id=tenant_id) as session:
        await session.execute(
            text(
                "update refresh_tokens set last_seen_at = now() - cast(:interval as interval) "
                "where family_id = :fid and revoked_at is null"
            ),
            {"fid": session_id, "interval": interval},
        )


async def _platform_family_reason(session_id: UUID) -> str | None:
    """平台 family 最新一行的撤销原因。"""

    async with db_session(platform=True) as session:
        value = await session.scalar(
            text(
                "select revoked_reason from platform_refresh_tokens "
                "where family_id = :fid order by issued_at desc limit 1"
            ),
            {"fid": session_id},
        )
    return str(value) if value is not None else None


async def _platform_active_family_count(user_id: UUID) -> int:
    """平台用户的活跃 family 数。"""

    async with db_session(platform=True) as session:
        return int(
            await session.scalar(
                text(
                    "select count(distinct family_id) from platform_refresh_tokens "
                    "where platform_user_id = :uid and revoked_at is null "
                    "and expires_at > now()"
                ),
                {"uid": user_id},
            )
            or 0
        )


async def _expect_unauthorized(client: AsyncClient, body: dict[str, object]) -> None:
    """断言访问令牌下一次请求即 401。"""

    response = await client.get("/api/v1/auth/me", headers=_headers(body))
    assert response.status_code == 401, response.text


async def test_bs_m1c2_full_session_management_chain(client: AsyncClient) -> None:
    """BS-48：四台登录踢最旧 → 列表 → 下线单台 → 退出其它 → 当前设备仍可用。"""

    first = await _login_device(client, CHROME_WINDOWS)
    second = await _login_device(client, SAFARI_IOS)
    third = await _login_device(client, FIREFOX_MAC)
    tenant_id = UUID(str(first["tenant_id"]))
    me = await client.get("/api/v1/auth/me", headers=_headers(first))
    user_id = UUID(me.json()["user_id"])

    # 固定"最久未用"顺序：第一台最旧、第二台次之
    sessions_before = await _list_sessions(client, third)
    by_label = {item["device_label"]: item["session_id"] for item in sessions_before}
    await _age_session(
        tenant_id, UUID(str(by_label["Chrome / Windows"])), timedelta(hours=2)
    )
    await _age_session(
        tenant_id, UUID(str(by_label["Safari / iOS"])), timedelta(hours=1)
    )

    # 第四台登录：超限踢掉最旧的第一台
    fourth = await _login_device(client, CHROME_ANDROID)

    assert await _active_family_count(tenant_id, user_id) == 3
    await _expect_unauthorized(client, first)
    refresh_first = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert refresh_first.status_code == 401
    assert "登录已失效" in refresh_first.json()["detail"]
    assert await _audit_rows_all("auth.refresh.reuse_detected") == []

    kicked = await _audit_rows("auth.session.kicked", tenant_id)
    assert len(kicked) == 1
    assert str(by_label["Chrome / Windows"]) in kicked[0]
    assert "Chrome / Windows" in kicked[0]

    # 设备列表：只剩 3 台，当前设备标记唯一
    listed = await _list_sessions(client, fourth)
    assert len(listed) == 3
    assert {item["device_label"] for item in listed} == {
        "Safari / iOS",
        "Firefox / macOS",
        "Chrome / Android",
    }
    assert sum(1 for item in listed if item["current"]) == 1
    current = next(item for item in listed if item["current"])
    assert current["device_label"] == "Chrome / Android"

    # 不存在/非本人的会话统一 404，不泄露归属
    missing = await client.delete(
        f"/api/v1/auth/sessions/{uuid4()}", headers=_headers(fourth)
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "会话不存在"

    # 下线单台：Firefox 立即失效，其它设备不受影响
    firefox_id = UUID(str(next(item["session_id"] for item in listed if item["device_label"] == "Firefox / macOS")))
    removed = await client.delete(f"/api/v1/auth/sessions/{firefox_id}", headers=_headers(fourth))
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"status": "ok"}
    await _expect_unauthorized(client, third)
    assert await _family_reason(tenant_id, firefox_id) == "session_revoked"
    assert len(await _audit_rows("auth.session.revoked", tenant_id)) == 1

    listed = await _list_sessions(client, fourth)
    assert len(listed) == 2

    # 退出其它设备：只保留当前（Chrome / Android）
    others = await client.post(
        "/api/v1/auth/sessions/revoke-others", headers=_headers(fourth)
    )
    assert others.status_code == 200, others.text
    assert others.json() == {"status": "ok", "revoked_count": 1}

    await _expect_unauthorized(client, second)
    assert (await client.get("/api/v1/auth/me", headers=_headers(fourth))).status_code == 200
    listed = await _list_sessions(client, fourth)
    assert [item["session_id"] for item in listed] == [current["session_id"]]

    revoked_others = await _audit_rows("auth.session.revoked_others", tenant_id)
    assert len(revoked_others) == 1
    assert str(current["session_id"]) in revoked_others[0]
    assert '"revoked_count": 1' in revoked_others[0] or '"revoked_count":1' in revoked_others[0]


async def test_bs_m1c2_revoke_current_clears_refresh_cookie(client: AsyncClient) -> None:
    """下线当前设备：清除刷新 Cookie，访问令牌与刷新令牌立即失效。"""

    body = await _login_device(client, CHROME_WINDOWS)
    tenant_id = UUID(str(body["tenant_id"]))
    listed = await _list_sessions(client, body)
    assert len(listed) == 1 and listed[0]["current"] is True
    session_id = UUID(str(listed[0]["session_id"]))

    removed = await client.delete(
        f"/api/v1/auth/sessions/{session_id}", headers=_headers(body)
    )
    assert removed.status_code == 200, removed.text
    set_cookie = removed.headers.get("set-cookie", "")
    assert "campus_refresh_token=" in set_cookie
    assert "Max-Age=0" in set_cookie

    await _expect_unauthorized(client, body)
    refreshed = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": body["refresh_token"]}
    )
    assert refreshed.status_code == 401
    assert await revocation.session_exists(session_id, realm="tenant") is False
    assert len(await _audit_rows("auth.session.revoked", tenant_id)) == 1


async def test_bs_m1c2_forced_password_change_blocks_session_management(
    client: AsyncClient,
) -> None:
    """首登改密门禁：会话接口 403；改密后可用。"""

    body = await _login_device(client, CHROME_WINDOWS, login_id="S-9102")
    assert body["must_change_password"] is True
    headers = _headers(body)

    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200
    for response in (
        await client.get("/api/v1/auth/sessions", headers=headers),
        await client.delete(f"/api/v1/auth/sessions/{uuid4()}", headers=headers),
        await client.post("/api/v1/auth/sessions/revoke-others", headers=headers),
    ):
        assert response.status_code == 403
        assert "修改密码" in response.json()["detail"]

    changed = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": TENANT_PASSWORD, "new_password": NEW_PASSWORD},
        headers=headers,
    )
    assert changed.status_code == 200, changed.text
    new_headers = {"Authorization": f"Bearer {changed.json()['access_token']}"}

    listed = await client.get("/api/v1/auth/sessions", headers=new_headers)
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["sessions"]) == 1
    assert listed.json()["sessions"][0]["current"] is True


async def test_bs_m1c2_platform_realm_limit_and_list(client: AsyncClient) -> None:
    """平台 realm：上限 2、第 3 台踢最旧；设备列表与退出其它走平台表。"""

    async def platform_login(user_agent: str) -> dict[str, object]:
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "realm": "platform",
                "account": "ops-sessions",
                "password": TEST_PLATFORM_PASSWORD,
            },
            headers={"user-agent": user_agent},
        )
        assert response.status_code == 200, response.text
        return dict(response.json())

    first = await platform_login(CHROME_WINDOWS)
    second = await platform_login(SAFARI_IOS)
    third = await platform_login(FIREFOX_MAC)

    me = await client.get("/api/v1/auth/me", headers=_headers(third))
    user_id = UUID(me.json()["user_id"])
    assert await _platform_active_family_count(user_id) == 2
    await _expect_unauthorized(client, first)
    assert len(await _platform_audit_rows("auth.session.kicked")) == 1

    listed = await _list_sessions(client, third)
    assert len(listed) == 2
    assert sum(1 for item in listed if item["current"]) == 1

    current = next(item for item in listed if item["current"])
    assert current["device_label"] == "Firefox / macOS"
    assert await _platform_family_reason(UUID(str(current["session_id"]))) is None

    others = await client.post(
        "/api/v1/auth/sessions/revoke-others", headers=_headers(third)
    )
    assert others.status_code == 200, others.text
    assert others.json()["revoked_count"] == 1
    await _expect_unauthorized(client, second)
    assert (await client.get("/api/v1/auth/me", headers=_headers(third))).status_code == 200


async def test_bs_m1c2_cannot_revoke_other_users_session(client: AsyncClient) -> None:
    """同租户内也不能查看/下线他人设备：按本人过滤，统一 404。"""

    alice = await _login_device(client, CHROME_WINDOWS, login_id="S-9101")
    bob = await _login_device(client, SAFARI_IOS, login_id="S-9103")
    tenant_id = UUID(str(alice["tenant_id"]))
    alice_session = UUID(str((await _list_sessions(client, alice))[0]["session_id"]))

    removed = await client.delete(
        f"/api/v1/auth/sessions/{alice_session}", headers=_headers(bob)
    )
    assert removed.status_code == 404
    assert removed.json()["detail"] == "会话不存在"
    assert await _family_reason(tenant_id, alice_session) is None
    assert (await client.get("/api/v1/auth/me", headers=_headers(alice))).status_code == 200
    assert await _audit_rows_all("auth.session.revoked") == []
    denied = await _audit_rows("auth.session.revoke_denied", tenant_id)
    assert len(denied) == 1
    assert str(alice_session) in denied[0]


async def test_bs_m1c2_logout_without_refresh_token_ends_session(
    client: AsyncClient,
) -> None:
    """只带访问令牌登出也必须撤销数据库 family，不能只删 Redis 标记。"""

    body = await _login_device(client, CHROME_WINDOWS)
    tenant_id = UUID(str(body["tenant_id"]))
    session_id = UUID(str((await _list_sessions(client, body))[0]["session_id"]))

    client.cookies.clear()
    response = await client.post("/api/v1/auth/logout", headers=_headers(body))
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok"}

    await _expect_unauthorized(client, body)
    refreshed = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": body["refresh_token"]}
    )
    assert refreshed.status_code == 401
    assert await _family_reason(tenant_id, session_id) == "logout"
    assert await revocation.session_exists(session_id, realm="tenant") is False

    logout_audit = await _audit_rows("auth.logout", tenant_id)
    assert len(logout_audit) == 1
    assert str(session_id) in logout_audit[0]
    assert '"family_revoked": true' in logout_audit[0] or '"family_revoked":true' in logout_audit[0]


async def test_bs_m1c2_logout_survives_redis_outage(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redis 故障时登出仍须撤销数据库 family、写一条审计并清除 Cookie。"""

    class _OutageRedis:
        async def set(self, *_args: object, **_kwargs: object) -> None:
            raise RedisError("redis down")

        async def delete(self, *_args: object, **_kwargs: object) -> int:
            raise RedisError("redis down")

        async def mget(self, *_args: object, **_kwargs: object) -> list[object]:
            raise RedisError("redis down")

    body = await _login_device(client, CHROME_WINDOWS)
    tenant_id = UUID(str(body["tenant_id"]))
    session_id = UUID(str((await _list_sessions(client, body))[0]["session_id"]))
    client.cookies.clear()
    monkeypatch.setattr(revocation, "_redis", lambda: _OutageRedis())

    response = await client.post("/api/v1/auth/logout", headers=_headers(body))
    assert response.status_code == 200, response.text
    assert await _family_reason(tenant_id, session_id) == "logout"
    refreshed = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": body["refresh_token"]}
    )
    assert refreshed.status_code == 401

    logout_audit = await _audit_rows("auth.logout", tenant_id)
    assert len(logout_audit) == 1
    assert str(session_id) in logout_audit[0]
    assert '"access_revoked": false' in logout_audit[0] or '"access_revoked":false' in logout_audit[0]


async def test_bs_m1c2_logout_with_bearer_and_refresh_audits_once(
    client: AsyncClient,
) -> None:
    """同时携带 Bearer 与有效刷新令牌登出，只写一条 auth.logout 审计。"""

    body = await _login_device(client, CHROME_WINDOWS)
    tenant_id = UUID(str(body["tenant_id"]))
    session_id = UUID(str((await _list_sessions(client, body))[0]["session_id"]))

    response = await client.post(
        "/api/v1/auth/logout",
        headers=_headers(body),
        json={"refresh_token": body["refresh_token"]},
    )
    assert response.status_code == 200, response.text
    assert await _family_reason(tenant_id, session_id) == "logout"
    audits = await _audit_rows("auth.logout", tenant_id)
    assert len(audits) == 1
    metadata = json.loads(audits[0])
    assert metadata["family_revoked"] is True
    assert metadata["subject_user_id"] is not None


async def test_bs_m1c2_logout_two_families_audits_each(
    client: AsyncClient,
) -> None:
    """Bearer 与刷新令牌属于不同设备时，两个 family 都撤销且各留一条审计。"""

    first = await _login_device(client, CHROME_WINDOWS)
    second = await _login_device(client, SAFARI_IOS)
    tenant_id = UUID(str(first["tenant_id"]))
    sessions = await _list_sessions(client, first)
    first_sid = UUID(
        str(
            next(
                item["session_id"]
                for item in sessions
                if item["device_label"] == "Chrome / Windows"
            )
        )
    )
    second_sid = UUID(
        str(
            next(
                item["session_id"]
                for item in sessions
                if item["device_label"] == "Safari / iOS"
            )
        )
    )

    response = await client.post(
        "/api/v1/auth/logout",
        headers=_headers(first),
        json={"refresh_token": second["refresh_token"]},
    )
    assert response.status_code == 200, response.text
    assert await _family_reason(tenant_id, first_sid) == "logout"
    assert await _family_reason(tenant_id, second_sid) == "logout"
    audits = await _audit_rows("auth.logout", tenant_id)
    assert len(audits) == 2
    combined = "\n".join(audits)
    assert str(first_sid) in combined
    assert str(second_sid) in combined
    await _expect_unauthorized(client, first)
    await _expect_unauthorized(client, second)


async def test_bs_m1c2_logout_audits_already_revoked_family_truthfully(
    client: AsyncClient,
) -> None:
    """刷新令牌所属 family 已被下线时，登出审计必须记录 family_revoked=false。"""

    first = await _login_device(client, CHROME_WINDOWS)
    second = await _login_device(client, SAFARI_IOS)
    tenant_id = UUID(str(first["tenant_id"]))
    sessions = await _list_sessions(client, first)
    first_sid = UUID(
        str(next(item["session_id"] for item in sessions if item["device_label"] == "Chrome / Windows"))
    )
    second_sid = UUID(
        str(next(item["session_id"] for item in sessions if item["device_label"] == "Safari / iOS"))
    )
    removed = await client.delete(
        f"/api/v1/auth/sessions/{second_sid}", headers=_headers(first)
    )
    assert removed.status_code == 200, removed.text

    response = await client.post(
        "/api/v1/auth/logout",
        headers=_headers(first),
        json={"refresh_token": second["refresh_token"]},
    )
    assert response.status_code == 200, response.text
    assert await _family_reason(tenant_id, first_sid) == "logout"
    assert await _family_reason(tenant_id, second_sid) == "session_revoked"

    audits = [json.loads(row) for row in await _audit_rows("auth.logout", tenant_id)]
    assert len(audits) == 2
    by_family = {item["family_id"]: item for item in audits}
    assert by_family[str(first_sid)]["family_revoked"] is True
    assert by_family[str(second_sid)]["family_revoked"] is False


async def test_bs_m1c2_logout_other_users_family_audits_subject(
    client: AsyncClient,
) -> None:
    """Bearer 属 A、刷新令牌属同租户 B 时，第二台设备的审计必须带 subject B。"""

    first = await _login_device(client, CHROME_WINDOWS)
    other = await _login_device(client, SAFARI_IOS, login_id="S-9103")
    tenant_id = UUID(str(first["tenant_id"]))
    actor_id = await _user_id_for(tenant_id, "S-9101")
    other_user_id = await _user_id_for(tenant_id, "S-9103")
    other_sid = UUID(str((await _list_sessions(client, other))[0]["session_id"]))

    response = await client.post(
        "/api/v1/auth/logout",
        headers=_headers(first),
        json={"refresh_token": other["refresh_token"]},
    )
    assert response.status_code == 200, response.text
    assert await _family_reason(tenant_id, other_sid) == "logout"

    rows = await _audit_rows_with_tenant("auth.logout", tenant_id)
    assert len(rows) == 2
    subject_tenant, subject_actor, subject_actor_realm, subject_metadata = next(
        (tenant, actor, actor_realm, metadata)
        for tenant, actor, actor_realm, metadata in rows
        if metadata["family_id"] == str(other_sid)
    )
    assert subject_tenant == tenant_id
    assert subject_actor == actor_id
    assert subject_actor_realm == "tenant"
    assert subject_metadata["subject_user_id"] == str(other_user_id)
    assert subject_metadata["family_revoked"] is True


async def test_bs_m1c2_cross_tenant_logout_audits_subject_tenant(
    client: AsyncClient,
) -> None:
    """Bearer 属租户 A、刷新令牌属租户 B 时，撤销审计必须落在 B 且带 subject。"""

    first = await _login_device(client, CHROME_WINDOWS)
    other = await _login_device(
        client, SAFARI_IOS, login_id="S-9201", school_code=SECOND_SCHOOL_CODE
    )
    tenant_a = UUID(str(first["tenant_id"]))
    tenant_b = UUID(str(other["tenant_id"]))
    assert tenant_a != tenant_b
    actor_id = await _user_id_for(tenant_a, "S-9101")
    other_user_id = await _user_id_for(tenant_b, "S-9201")
    first_sid = UUID(str((await _list_sessions(client, first))[0]["session_id"]))
    other_sid = UUID(str((await _list_sessions(client, other))[0]["session_id"]))

    response = await client.post(
        "/api/v1/auth/logout",
        headers=_headers(first),
        json={"refresh_token": other["refresh_token"]},
    )
    assert response.status_code == 200, response.text
    assert await _family_reason(tenant_a, first_sid) == "logout"
    assert await _family_reason(tenant_b, other_sid) == "logout"

    rows = await _audit_rows_with_tenant("auth.logout", tenant_a, tenant_b)
    assert len(rows) == 2
    by_family = {
        metadata["family_id"]: (tenant, actor, actor_realm, metadata)
        for tenant, actor, actor_realm, metadata in rows
    }
    first_tenant, first_actor, first_actor_realm, _first_metadata = by_family[str(first_sid)]
    assert first_tenant == tenant_a
    assert first_actor == actor_id
    assert first_actor_realm == "tenant"
    subject_tenant, subject_actor, subject_actor_realm, subject_metadata = by_family[
        str(other_sid)
    ]
    assert subject_tenant == tenant_b
    assert subject_actor == actor_id
    assert subject_actor_realm == "tenant"
    assert subject_metadata["subject_user_id"] == str(other_user_id)
    assert subject_metadata["family_revoked"] is True
    assert subject_metadata["access_revoked"] is False
