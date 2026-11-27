"""M1-E1 集成测试：C 端对话接口的鉴权边界与权限热更新（真实 PG + Redis）。

断言的是业务结果，不是「接口返回 200」：
- 未认证 / 缺权限 / realm 不符分别落在 401、403、403，且缺权限与 realm 不符都写审计；
- 放行时 SSE 正常产出，trace 的租户与用户身份来自认证主体（不串租）；
- 角色↔权限映射改动**下一次请求即生效**，且不影响未过期的访问令牌（不 bump token_version）。
"""

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth import revocation
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.harness.trace import InMemoryTraceSink
from app.main import create_app
from tests.support.auth import (
    TEST_PLATFORM_PASSWORD,
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
from tests.support.llm import AllowRouteFakeLlm
from tests.support.metrics import counter_value

pytestmark = [pytest.mark.integration]

ENDPOINT = "/api/v1/campus/chat/turns"
CHAT_PERMISSION = "campus.chat.use"
SCHOOL_CODE = "SCH-E1CHAT"
OTHER_SCHOOL_CODE = "SCH-E1CHAT2"
TENANT_PASSWORD = "Chat-Passw0rd-1"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    """确保测试库结构最新，并对清理连接做一次性库护栏。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def chat_env(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, InMemoryTraceSink]]:
    """指向测试栈的应用客户端（开关打开）+ 注入的内存 trace sink。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
        await seed_tenant_user(
            admin_engine,
            school_code=SCHOOL_CODE,
            login_id="S-1001",
            password=TENANT_PASSWORD,
            user_type="student",
            permissions=(CHAT_PERMISSION,),
        )
        await seed_tenant_user(
            admin_engine,
            school_code=SCHOOL_CODE,
            login_id="S-1002",
            password=TENANT_PASSWORD,
            user_type="student",
            permissions=("school.dashboard.view",),
        )
        await seed_tenant_user(
            admin_engine,
            school_code=OTHER_SCHOOL_CODE,
            login_id="S-2001",
            password=TENANT_PASSWORD,
            user_type="student",
            permissions=(CHAT_PERMISSION,),
        )
        # 故意给平台账号授予 C 端码：realm 边界必须**独立于权限**成立，
        # 否则这条测试只是在测"缺权限"，测不到"跨 realm"。
        await seed_platform_user(admin_engine, account="ops-chat", permissions=(CHAT_PERMISSION,))
    finally:
        await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    get_settings.cache_clear()
    await revocation.reset_client()

    sink = InMemoryTraceSink()
    transport = ASGITransport(app=create_app(trace_sink=sink, llm_port=AllowRouteFakeLlm()))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, sink

    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()


async def _audit_rows() -> list[dict[str, Any]]:
    """读取权限拒绝审计的 (action, metadata_json, tenant_id, actor_id)。

    带上 `tenant_id`/`actor_id`：拒绝审计只有落对主体与租户才谈得上可追溯（第四轮审查 Minor 2）。
    """

    async with db_session(platform=True) as session:
        rows = (
            await session.execute(
                text(
                    "select action, metadata_json::text as metadata, "
                    "coalesce(tenant_id::text, '') as tenant_id, "
                    "coalesce(actor_id::text, '') as actor_id "
                    "from audit_logs where action = 'auth.permission.denied'"
                )
            )
        ).mappings().all()
    return [
        {
            "action": str(row["action"]),
            "metadata": str(row["metadata"]),
            "tenant_id": str(row["tenant_id"]),
            "actor_id": str(row["actor_id"]),
        }
        for row in rows
    ]


async def _identity(school_code: str, login_id: str) -> tuple[UUID, UUID]:
    """按学校码与登录号读取 (tenant_id, user_id)。"""

    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "select u.tenant_id, u.id from users u join tenants t on t.id = u.tenant_id "
                        "where t.school_code = :code and u.login_id = :login_id"
                    ),
                    {"code": school_code, "login_id": login_id},
                )
            ).mappings().one()
        return UUID(str(row["tenant_id"])), UUID(str(row["id"]))
    finally:
        await engine.dispose()


async def _revoke_permission(engine: AsyncEngine, *, login_id: str) -> None:
    """删除某租户用户测试角色的全部权限映射（模拟管理员在前台收回权限）。"""

    factory = make_session_factory(engine)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "delete from role_permissions where role_id in "
                "(select ur.role_id from user_roles ur join users u on u.id = ur.user_id "
                "where u.login_id = :login_id)"
            ),
            {"login_id": login_id},
        )


async def _grant_permission(engine: AsyncEngine, *, login_id: str) -> None:
    """把 C 端对话权限重新授予该用户的测试角色（模拟前台重新配置）。"""

    factory = make_session_factory(engine)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "insert into role_permissions (tenant_id, role_id, permission_id) "
                "select ur.tenant_id, ur.role_id, p.id "
                "from user_roles ur join users u on u.id = ur.user_id "
                "cross join permissions p "
                "where u.login_id = :login_id and p.code = :code "
                "on conflict do nothing"
            ),
            {"login_id": login_id, "code": CHAT_PERMISSION},
        )


async def _token(client: AsyncClient, login_id: str) -> dict[str, str]:
    """登录并返回 Authorization 头。"""

    return await login_tenant(client, school_code=SCHOOL_CODE, login_id=login_id, password=TENANT_PASSWORD)


@pytest.mark.asyncio
async def test_anonymous_request_is_rejected_without_trace(
    chat_env: tuple[AsyncClient, InMemoryTraceSink]
) -> None:
    """未认证：401 且不产生 trace（空壳不再是无认证入口）。"""

    client, sink = chat_env
    response = await client.post(ENDPOINT, json={"message": "匿名请求"})

    assert response.status_code == 401
    assert sink.turns == []


@pytest.mark.asyncio
async def test_tenant_user_without_permission_is_forbidden_and_audited(
    chat_env: tuple[AsyncClient, InMemoryTraceSink]
) -> None:
    """有租户会话但缺 `campus.chat.use`：403、无 trace、写权限拒绝审计（含原因与路径）。"""

    client, sink = chat_env
    tenant_id, user_id = await _identity(SCHOOL_CODE, "S-1002")
    headers = await _token(client, "S-1002")
    denials_before = counter_value(
        "campus_permission_denials_total", {"realm": "tenant", "reason": "missing_permission"}
    )
    response = await client.post(ENDPOINT, json={"message": "无权限提问"}, headers=headers)

    assert response.status_code == 403
    assert (
        counter_value(
            "campus_permission_denials_total",
            {"realm": "tenant", "reason": "missing_permission"},
        )
        == denials_before + 1
    )
    assert CHAT_PERMISSION in response.json()["detail"]
    assert sink.turns == []

    denied = [row for row in await _audit_rows() if row["actor_id"] == str(user_id)]
    assert denied, "权限拒绝必须留审计，且认领到被拒的主体"
    assert {row["tenant_id"] for row in denied} == {str(tenant_id)}, (
        f"拒绝审计的租户归属不对：{denied}"
    )
    assert any("missing_permission" in row["metadata"] for row in denied)
    assert any(ENDPOINT in row["metadata"] for row in denied)


@pytest.mark.asyncio
async def test_authorized_turn_streams_and_traces_principal_identity(
    chat_env: tuple[AsyncClient, InMemoryTraceSink]
) -> None:
    """持 `campus.chat.use` 的学生：SSE 走通，trace 身份为该主体自己的租户与用户。"""

    client, sink = chat_env
    tenant_id, user_id = await _identity(SCHOOL_CODE, "S-1001")
    headers = await _token(client, "S-1001")

    response = await client.post(
        ENDPOINT, json={"message": "图书馆几点开门", "conversation_id": "conv-e1"}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: meta" in response.text
    assert "event: done" in response.text

    assert len(sink.turns) == 1
    assert sink.turns[0].tenant_id == tenant_id
    assert sink.turns[0].user_id == user_id


@pytest.mark.asyncio
async def test_other_tenant_identity_is_not_leaked_into_trace(
    chat_env: tuple[AsyncClient, InMemoryTraceSink]
) -> None:
    """另一租户的学生：trace 用自己的 tenant_id，不落到第一个租户（占位身份的回归防线）。"""

    client, sink = chat_env
    other_tenant, other_user = await _identity(OTHER_SCHOOL_CODE, "S-2001")
    first_tenant, _ = await _identity(SCHOOL_CODE, "S-1001")
    login = await login_tenant(
        client, school_code=OTHER_SCHOOL_CODE, login_id="S-2001", password=TENANT_PASSWORD
    )

    response = await client.post(ENDPOINT, json={"message": "图书馆几点开门"}, headers=login)

    assert response.status_code == 200, response.text
    record = sink.turns[-1]
    assert record.tenant_id == other_tenant
    assert record.user_id == other_user
    assert record.tenant_id != first_tenant


@pytest.mark.asyncio
async def test_platform_token_cannot_use_campus_chat(
    chat_env: tuple[AsyncClient, InMemoryTraceSink]
) -> None:
    """realm 边界：平台令牌即使权限目录里有全部码，也不能访问 C 端接口（realm 不符 403）。"""

    client, sink = chat_env
    headers = await login_platform(client, "ops-chat")
    denials_before = counter_value(
        "campus_permission_denials_total", {"realm": "platform", "reason": "realm_mismatch"}
    )
    response = await client.post(ENDPOINT, json={"message": "平台令牌提问"}, headers=headers)

    assert response.status_code == 403
    assert (
        counter_value(
            "campus_permission_denials_total",
            {"realm": "platform", "reason": "realm_mismatch"},
        )
        == denials_before + 1
    )
    # realm 判定先于权限判定：该账号已被授予 `campus.chat.use`，
    # 若这里出现"缺少权限"说明测的是权限缺失而不是跨 realm 拦截。
    assert response.json()["detail"] != f"缺少权限：{CHAT_PERMISSION}"
    assert sink.turns == []
    denied = await _audit_rows()
    assert any(
        "realm_mismatch" in row["metadata"] and not row["tenant_id"] for row in denied
    ), f"平台主体的拒绝审计必须存在且不认领任何租户：{denied}"
    assert TEST_PLATFORM_PASSWORD not in str(denied)


@pytest.mark.asyncio
async def test_revoked_permission_blocks_next_request_with_same_token(
    chat_env: tuple[AsyncClient, InMemoryTraceSink]
) -> None:
    """热更新：收回映射后**同一个未过期访问令牌**下一次请求即 403，且令牌本身仍有效。"""

    client, sink = chat_env
    tenant_id, _ = await _identity(SCHOOL_CODE, "S-1001")
    headers = await _token(client, "S-1001")
    allowed = await client.post(ENDPOINT, json={"message": "改前"}, headers=headers)
    assert allowed.status_code == 200, allowed.text

    engine = make_test_engine("admin")
    try:
        await _revoke_permission(engine, login_id="S-1001")
    finally:
        await engine.dispose()

    denied = await client.post(ENDPOINT, json={"message": "改后"}, headers=headers)
    assert denied.status_code == 403
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200

    async with db_session(tenant_id=tenant_id) as session:
        token_version = await session.scalar(
            text("select token_version from users where login_id = :login_id"),
            {"login_id": "S-1001"},
        )
    assert int(str(token_version)) == 0, "权限热更新不应吊销会话（不动 token_version）"

    # 重新授予：同样无需重新登录即可恢复
    engine = make_test_engine("admin")
    try:
        await _grant_permission(engine, login_id="S-1001")
    finally:
        await engine.dispose()
    restored = await client.post(ENDPOINT, json={"message": "恢复"}, headers=headers)
    assert restored.status_code == 200, restored.text
    assert len(sink.turns) == 2, "只统计放行的两个回合（403 不产生 trace）"


@pytest.mark.asyncio
async def test_invalid_body_is_422_after_authorization_and_writes_no_trace(
    chat_env: tuple[AsyncClient, InMemoryTraceSink]
) -> None:
    """已鉴权但请求体非法：422，且不进入处理器、不产生 trace。"""

    client, sink = chat_env
    headers = await _token(client, "S-1001")
    before = len(sink.turns)
    response = await client.post(ENDPOINT, json={"message": ""}, headers=headers)

    assert response.status_code == 422
    assert len(sink.turns) == before
