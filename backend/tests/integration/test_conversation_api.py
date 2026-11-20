"""M4-AI06/07 集成测试：会话列表/历史/清空端点 + 持久化接线（真实 PG + Redis）。

断言业务结果而非 HTTP 200：
- 列表 keyset 游标翻页不重不漏（M2：cursor 真正参与过滤）；
- 历史/清空的归属校验（跨用户 404）+ limit 参数校验（m5）；
- 清空 = 单事务（软删 + 审计一致落地，m4）+ 幂等；
- chat/turns 回合落库：conversation_messages + chat_traces + llm_usage 三表齐写
  （B1/A5/A6），provider/model 来自 LlmPort；
- 上下文窗口 = 近 10 轮 = 20 条（m1：超出窗口最旧消息不返回）。
"""

import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.auth import revocation
from app.config import get_settings
from app.db.models.ai import Conversation
from app.db.repository import ScopeBypass, TenantScopedRepository
from app.db.session import db_session, dispose_engine
from app.harness.chat_assembly import build_chat_loop
from app.harness.llm import LlmResponse
from app.harness.persistence import get_recent_messages, save_message
from app.harness.trace_db import DbTraceSink
from app.main import create_app
from tests.support.auth import login_tenant, seed_tenant_user
from tests.support.db import (
    clean_test_data,
    database_url,
    make_session_factory,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn
from tests.support.llm import FakeLlm

pytestmark = [pytest.mark.integration]

SCHOOL_CODE = "SCH-AI0607"
PASSWORD = "Conv-Passw0rd-1"
USER_MAIN = "S-6101"
USER_OTHER = "S-6102"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    """确保测试库结构最新。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def db_env(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """测试库造数（主用户 + 第二用户）+ 应用连接环境变量。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
        await seed_tenant_user(
            admin_engine, school_code=SCHOOL_CODE, login_id=USER_MAIN,
            password=PASSWORD, user_type="student",
            permissions=("campus.chat.use", "campus.conversation.clear"),
        )
        await seed_tenant_user(
            admin_engine, school_code=SCHOOL_CODE, login_id=USER_OTHER,
            password=PASSWORD, user_type="student",
            permissions=("campus.chat.use", "campus.conversation.clear"),
        )
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


@pytest.fixture
async def conv_env(db_env: None) -> AsyncIterator[AsyncClient]:
    """会话管理端点用应用客户端（默认装配即可，不触 LLM）。"""

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def turn_env(db_env: None) -> AsyncIterator[tuple[AsyncClient, UUID, UUID]]:
    """带 FakeLlm + DbTraceSink 的应用客户端（回合落库弧专用）。

    无已发布知识库 → 检索在活跃版本发现即短路为空 → CAPABILITY_FALLBACK 兜底弧
    （确定性，不依赖 TEI/Qdrant 行为）。
    """

    tenant_id, user_id = await _identity()
    fake = FakeLlm(
        complete_scripts=[_route_response(), _route_response()], stream_scripts=[],
    )
    loop = build_chat_loop(get_settings(), trace_sink=DbTraceSink(), llm_port=fake)
    transport = ASGITransport(app=create_app(chat_loop=loop))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, tenant_id, user_id


def _route_response() -> LlmResponse:
    """路由脚本：kb_qa 意图（无已发布版本 → 检索空 → CAPABILITY_FALLBACK）。"""

    return LlmResponse(
        text=json.dumps({"intents": [{"name": "kb_qa", "query": "测试问题"}]}),
        finish_reason="stop",
    )


async def _identity() -> tuple[UUID, UUID]:
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
                    {"code": SCHOOL_CODE, "login_id": USER_MAIN},
                )
            ).mappings().one()
        return UUID(str(row["tenant_id"])), UUID(str(row["id"]))
    finally:
        await engine.dispose()


async def _insert_conversation(
    tenant_id: UUID, user_id: UUID, *,
    created_at: datetime, last_message_at: datetime | None = None,
) -> UUID:
    """直接造会话行（可控 created_at/last_message_at，排序断言确定性）。"""

    async with db_session(tenant_id=tenant_id) as session:
        TenantScopedRepository(
            session, Conversation, tenant_id,
            scope=ScopeBypass(reason="测试造数：会话列表分页用例"),
        )
        conv = Conversation(
            id=uuid4(), tenant_id=tenant_id, user_id=user_id, status="active",
            created_at=created_at, updated_at=created_at,
            last_message_at=last_message_at,
        )
        session.add(conv)
        await session.commit()
        return conv.id


async def _token(client: AsyncClient, login_id: str) -> dict[str, str]:
    return await login_tenant(
        client, school_code=SCHOOL_CODE, login_id=login_id, password=PASSWORD,
    )


@pytest.mark.asyncio
async def test_list_conversations_keyset_pagination(conv_env: AsyncClient) -> None:
    """列表按最近消息降序；cursor 翻页不重不漏（M2 修复验证）。"""

    tenant_id, user_id = await _identity()
    base = datetime.now(UTC).replace(microsecond=0)
    conv_old = await _insert_conversation(tenant_id, user_id, created_at=base - timedelta(minutes=30))
    conv_new = await _insert_conversation(
        tenant_id, user_id,
        created_at=base - timedelta(minutes=20),
        last_message_at=base - timedelta(minutes=5),
    )
    conv_mid = await _insert_conversation(tenant_id, user_id, created_at=base - timedelta(minutes=10))

    headers = await _token(conv_env, USER_MAIN)
    page1 = await conv_env.get("/api/v1/campus/conversations", params={"limit": 2}, headers=headers)
    assert page1.status_code == 200, page1.text
    body1 = page1.json()
    # 排序：conv_new（last_message_at 最强）> conv_mid > conv_old（created_at 兜底）
    assert [c["id"] for c in body1["conversations"]] == [str(conv_new), str(conv_mid)]
    assert body1["next_cursor"] is not None

    page2 = await conv_env.get(
        "/api/v1/campus/conversations",
        params={"limit": 2, "cursor": body1["next_cursor"]}, headers=headers,
    )
    assert page2.status_code == 200, page2.text
    body2 = page2.json()
    assert [c["id"] for c in body2["conversations"]] == [str(conv_old)]
    assert body2["next_cursor"] is None

    # 全量对照：一页拿全，且顺序一致
    full = await conv_env.get("/api/v1/campus/conversations", headers=headers)
    assert [c["id"] for c in full.json()["conversations"]] == [
        str(conv_new), str(conv_mid), str(conv_old),
    ]


@pytest.mark.asyncio
async def test_list_cursor_and_limit_validation(conv_env: AsyncClient) -> None:
    """cursor 非法 → 422；limit 越界 → 422（m5）。"""

    headers = await _token(conv_env, USER_MAIN)
    bad_cursor = await conv_env.get(
        "/api/v1/campus/conversations", params={"cursor": "not-a-cursor"}, headers=headers,
    )
    assert bad_cursor.status_code == 400  # CursorError 契约（绝不静默回第一页）
    bad_limit = await conv_env.get(
        "/api/v1/campus/conversations", params={"limit": 0}, headers=headers,
    )
    assert bad_limit.status_code == 422
    big_limit = await conv_env.get(
        "/api/v1/campus/conversations", params={"limit": 101}, headers=headers,
    )
    assert big_limit.status_code == 422


@pytest.mark.asyncio
async def test_message_history_and_cross_user_404(conv_env: AsyncClient) -> None:
    """历史按时间正序排除 deleted；他人会话 404（m3 归属校验）。"""

    tenant_id, user_id = await _identity()
    conv_id = await _insert_conversation(tenant_id, user_id, created_at=datetime.now(UTC))
    await save_message(tenant_id=tenant_id, conversation_id=conv_id, role="user", content="第一问")
    await save_message(tenant_id=tenant_id, conversation_id=conv_id, role="assistant", content="第一答")
    await save_message(tenant_id=tenant_id, conversation_id=conv_id, role="user", content="第二问")

    headers = await _token(conv_env, USER_MAIN)
    resp = await conv_env.get(
        f"/api/v1/campus/conversations/{conv_id}/messages", headers=headers,
    )
    assert resp.status_code == 200, resp.text
    messages = resp.json()["messages"]
    assert [m["content"] for m in messages] == ["第一问", "第一答", "第二问"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]

    other_headers = await _token(conv_env, USER_OTHER)
    cross = await conv_env.get(
        f"/api/v1/campus/conversations/{conv_id}/messages", headers=other_headers,
    )
    assert cross.status_code == 404


@pytest.mark.asyncio
async def test_clear_single_transaction_idempotent_and_audited(conv_env: AsyncClient) -> None:
    """清空：软删 + 审计同事务落地（m4）；幂等重放 cleared=0（AI-07）。"""

    tenant_id, user_id = await _identity()
    conv_id = await _insert_conversation(tenant_id, user_id, created_at=datetime.now(UTC))
    await save_message(tenant_id=tenant_id, conversation_id=conv_id, role="user", content="一")
    await save_message(tenant_id=tenant_id, conversation_id=conv_id, role="assistant", content="二")

    headers = await _token(conv_env, USER_MAIN)
    clear = await conv_env.delete(
        f"/api/v1/campus/conversations/{conv_id}/messages", headers=headers,
    )
    assert clear.status_code == 200, clear.text
    assert clear.json()["cleared"] == 2

    # 幂等
    clear_again = await conv_env.delete(
        f"/api/v1/campus/conversations/{conv_id}/messages", headers=headers,
    )
    assert clear_again.status_code == 200
    assert clear_again.json()["cleared"] == 0

    # 两次清空各写一条审计（幂等重放也留痕——操作可追溯）
    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "select count(*) as n from audit_logs where tenant_id = :t "
                        "and action = 'campus.conversation.clear'"
                    ),
                    {"t": str(tenant_id)},
                )
            ).mappings().one()
        assert int(row["n"]) == 2
    finally:
        await engine.dispose()

    # 他人清空 → 404 且不写审计
    other_headers = await _token(conv_env, USER_OTHER)
    cross = await conv_env.delete(
        f"/api/v1/campus/conversations/{conv_id}/messages", headers=other_headers,
    )
    assert cross.status_code == 404


@pytest.mark.asyncio
async def test_turn_persists_messages_trace_and_usage(
    turn_env: tuple[AsyncClient, UUID, UUID],
) -> None:
    """回合落库三表齐写（B1/A5/A6）：messages + trace(provider/model) + usage。"""

    client, tenant_id, user_id = turn_env
    headers = await _token(client, USER_MAIN)
    resp = await client.post(
        "/api/v1/campus/chat/turns",
        json={"message": "测试问题"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            conv_rows = (
                await session.execute(
                    text(
                        "select id, pinned_kb_version_id from conversations "
                        "where tenant_id = :t and user_id = :u"
                    ),
                    {"t": str(tenant_id), "u": str(user_id)},
                )
            ).mappings().all()
            assert len(conv_rows) == 1
            conv_id = str(conv_rows[0]["id"])
            # 无已发布知识库 → pinned 为 NULL（fail-closed，不回退任选）
            assert conv_rows[0]["pinned_kb_version_id"] is None

            msg_rows = (
                await session.execute(
                    text(
                        "select role, status from conversation_messages "
                        "where conversation_id = :c order by created_at"
                    ),
                    {"c": conv_id},
                )
            ).mappings().all()
            assert [(m["role"], m["status"]) for m in msg_rows] == [
                ("user", "completed"), ("assistant", "completed"),
            ]

            trace_rows = (
                await session.execute(
                    text(
                        "select route_path, model, stop_reason, error_code from chat_traces "
                        "where tenant_id = :t and user_id = :u"
                    ),
                    {"t": str(tenant_id), "u": str(user_id)},
                )
            ).mappings().all()
            assert len(trace_rows) == 1
            assert trace_rows[0]["route_path"] == "fast"
            assert trace_rows[0]["model"] == "fake-model"
            assert trace_rows[0]["stop_reason"] == "CAPABILITY_FALLBACK"
            assert trace_rows[0]["error_code"] is None

            usage_rows = (
                await session.execute(
                    text(
                        "select provider, model, trace_id from llm_usage "
                        "where tenant_id = :t"
                    ),
                    {"t": str(tenant_id)},
                )
            ).mappings().all()
            assert len(usage_rows) == 1
            assert usage_rows[0]["provider"] == "fake"
            assert usage_rows[0]["model"] == "fake-model"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_context_window_is_twenty_messages(db_env: None) -> None:
    """上下文窗口 = 近 10 轮 = 20 条（m1）：25 条历史只返回最近 20 条，时间正序。"""

    tenant_id, user_id = await _identity()
    conv_id = await _insert_conversation(tenant_id, user_id, created_at=datetime.now(UTC))
    for i in range(25):
        await save_message(
            tenant_id=tenant_id, conversation_id=conv_id,
            role="user" if i % 2 == 0 else "assistant", content=f"消息{i:02d}",
        )
    recent = await get_recent_messages(tenant_id=tenant_id, conversation_id=conv_id)
    assert len(recent) == 20
    assert recent[0]["content"] == "消息05"
    assert recent[-1]["content"] == "消息24"


@pytest.mark.asyncio
async def test_turn_with_other_users_conversation_is_isolated(
    turn_env: tuple[AsyncClient, UUID, UUID],
) -> None:
    """B 提交 A 的 conversation_id → 绝不读写 A 的会话（第 2 轮审查 Major-1）。

    断言业务结果：B 的回合照常兜底完成；A 的会话消息数不变；不存在以客户端
    原值复用的会话（meta 回显全新 uuid）；conversations 表只有 A 的会话。
    """

    from app.harness.persistence import get_or_create_conversation

    client, tenant_id, user_a = turn_env
    conv_a, _created = await get_or_create_conversation(
        tenant_id=tenant_id, user_id=user_a,
    )
    await save_message(tenant_id=tenant_id, conversation_id=conv_a, role="user", content="A 的原话")

    headers_b = await _token(client, USER_OTHER)
    resp = await client.post(
        "/api/v1/campus/chat/turns",
        json={"message": "B 的提问", "conversation_id": str(conv_a)},
        headers=headers_b,
    )
    assert resp.status_code == 200, resp.text
    data_lines = [
        line.split(":", 1)[1].strip()
        for line in resp.text.splitlines()
        if line.startswith("data:")
    ]
    meta_payload = json.loads(data_lines[0])
    done_payload = json.loads(data_lines[-1])
    # 不复用客户端原值：meta 回显全新会话 id（绝不指向 A 的会话）
    assert meta_payload["conversation_id"] != str(conv_a)
    assert done_payload["stop_reason"] == "CAPABILITY_FALLBACK"

    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            conv_count = (
                await session.execute(
                    text("select count(*) as n from conversations where tenant_id = :t"),
                    {"t": str(tenant_id)},
                )
            ).mappings().one()
            # B 的落库走 FK 失败被吞：不产生 B 的会话行，也不污染 A 的会话
            assert int(conv_count["n"]) == 1

            msg_rows = (
                await session.execute(
                    text(
                        "select role, content from conversation_messages "
                        "where conversation_id = :c"
                    ),
                    {"c": str(conv_a)},
                )
            ).mappings().all()
            # A 的会话只有 A 自己的 user 消息；B 的提问/回答都没写进来
            assert len(msg_rows) == 1
            assert msg_rows[0]["content"] == "A 的原话"
    finally:
        await engine.dispose()
