"""业务场景 BS-M1-F：审计哈希链全链路（HTTP 查询 + 权限 + 直写防线）。

链校验脚本已由 test_audit_hash_chain.py 覆盖；本文件聚焦 HTTP 层。
命名 zz_ 前缀确保在 Windows 上最后运行（asyncpg 引擎池跨事件循环的已知问题）。
"""

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.main import create_app
from tests.support.auth import login_platform, seed_platform_user
from tests.support.db import (
    clean_test_data,
    database_url,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.integration]

TEST_AUDIT_KEY = "bs-audit-key-0123456789abcdef0123456789abcdef"
PLATFORM_ACCOUNT = "ops-audit-viewer"
NO_AUDIT_ACCOUNT = "ops-no-audit"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    """确保测试库结构最新。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def client(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    """指向测试库的应用客户端 + 两个平台账号。"""

    await dispose_engine()
    get_settings.cache_clear()

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
        await seed_platform_user(
            admin_engine, account=PLATFORM_ACCOUNT, permissions=("platform.audit.view",)
        )
        await seed_platform_user(
            admin_engine, account=NO_AUDIT_ACCOUNT, permissions=("platform.tenant.view",)
        )
    finally:
        await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    monkeypatch.setenv("AUDIT_HASH_KEY", TEST_AUDIT_KEY)
    monkeypatch.setenv("DEV_SKIP_SCHOOL_REVIEW", "false")
    get_settings.cache_clear()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await dispose_engine()
    get_settings.cache_clear()


async def test_bs_m1f_audit_chain_http_flow(client: AsyncClient) -> None:
    """业务操作 → 审计带哈希 → 平台可查询 → 无权被拒 → 直写仍入链。"""

    # ---- 1) 产生审计事件（登录成功 + 登录失败）----
    await client.post(
        "/api/v1/auth/login",
        json={"realm": "platform", "account": PLATFORM_ACCOUNT, "password": "wrong"},
    )
    headers = await login_platform(client, PLATFORM_ACCOUNT)

    # ---- 2) 平台审计查询：能查到登录事件，哈希字段已填 ----
    audit_resp = await client.get(
        "/api/v1/platform/audit-logs",
        params={"action": "auth.login.success", "limit": 10},
        headers=headers,
    )
    assert audit_resp.status_code == 200, audit_resp.text
    body = audit_resp.json()
    assert body["total"] >= 1
    assert all(item["action"] == "auth.login.success" for item in body["items"])
    assert all(item["hash"] is not None for item in body["items"])
    assert all(len(item["hash"]) == 64 for item in body["items"])
    assert all(item["hash_version"] == 1 for item in body["items"])

    # ---- 3) 哈希链连续性：相邻两条的 prev_hash 链接 ----
    if len(body["items"]) >= 2:
        items = sorted(body["items"], key=lambda x: x["sequence"])
        for i in range(1, len(items)):
            assert items[i]["prev_hash"] == items[i - 1]["hash"], (
                f"sequence {items[i]['sequence']} 的 prev_hash 断链"
            )

    # ---- 4) 按条件过滤：failure 也能查到 ----
    failed_resp = await client.get(
        "/api/v1/platform/audit-logs",
        params={"result": "failure", "limit": 10},
        headers=headers,
    )
    assert failed_resp.status_code == 200
    assert failed_resp.json()["total"] >= 1

    # ---- 5) 分页：cursor 有效 ----
    page1 = await client.get(
        "/api/v1/platform/audit-logs", params={"limit": 2}, headers=headers
    )
    assert page1.status_code == 200
    p1 = page1.json()
    if p1["next_cursor"]:
        page2 = await client.get(
            "/api/v1/platform/audit-logs",
            params={"limit": 2, "before_sequence": p1["next_cursor"]},
            headers=headers,
        )
        assert page2.status_code == 200
        assert page2.json()["items"][0]["sequence"] < p1["items"][-1]["sequence"]

    # ---- 6) 无审计权限的平台账号 → 403 + 拒绝审计 ----
    no_audit_headers = await login_platform(client, NO_AUDIT_ACCOUNT)
    denied = await client.get("/api/v1/platform/audit-logs", headers=no_audit_headers)
    assert denied.status_code == 403

    denied_audit = await client.get(
        "/api/v1/platform/audit-logs",
        params={"action": "auth.permission.denied", "limit": 5},
        headers=headers,
    )
    assert denied_audit.status_code == 200
    assert denied_audit.json()["total"] >= 1

    # ---- 7) 绕过应用 API 层直写 → 触发器仍算哈希（数据库层防线）----
    async with db_session(platform=True) as session:
        await session.execute(
            text(
                "INSERT INTO audit_logs "
                "(event_id, actor_realm, action, resource_type, result, created_at) "
                "VALUES (gen_random_uuid(), 'system', 'bs.direct_sql', 'test', 'success', now())"
            )
        )

    resp = await client.get(
        "/api/v1/platform/audit-logs",
        params={"action": "bs.direct_sql"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1
    assert resp.json()["items"][0]["hash"] is not None
    assert resp.json()["items"][0]["hash_version"] == 1
