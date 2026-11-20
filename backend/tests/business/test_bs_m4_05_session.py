"""BS-M4-05：会话持久化多轮对话业务场景（M4-AI06/07；真实 PG+Qdrant+TEI + FakeLlm）。

Given 已发布知识库 + 学生身份；When 多轮提问；Then：
1. 消息持久化（user+assistant 写入 conversation_messages）
2. 上下文注入（第二轮的 LLM prompt 含第一轮问答）
3. 清空会话（AI-07：软删 + 审计 + 后续轮不带入已清空消息）
MinerU 不需要；TEI 不可达即整模块 skip（CI 无容器）。
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import get_settings
from app.harness.chat_assembly import build_chat_loop
from app.harness.llm import LlmResponse, LlmStreamEvent
from app.kb.build_task import build_kb_version
from app.kb.parser import ParsedDocument
from tests.support.auth import seed_tenant_user
from tests.support.db import make_session_factory, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_audience_seed import configure_audiences
from tests.support.kb_env import (
    KB_APPROVER,
    KB_BUILDER,
    KB_PUBLISHER,
    PASSWORD,
    UPLOADER,
    KbEnv,
    kb_env,
    login,
    rows,
    upload,
)
from tests.support.kb_publish_seed import FakeParser, patch_ports
from tests.support.llm import FakeLlm

pytestmark = [pytest.mark.integration]

MARKDOWN = """# 财务报销手册

## 第一章 差旅报销

差旅报销需要发票和审批单。

## 第二章 招待报销

招待费报销需注明事由。
"""

STUDENT_LOGIN = "bs-m4-session-student"


@pytest.fixture(scope="module")
def engines_available() -> Iterator[bool]:
    async def probe() -> bool:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5.0) as probe_client:
                response = await probe_client.get(f"{get_settings().tei_embed_url}/health")
            return bool(response.status_code < 500)
        except Exception:
            return False
    tei = asyncio.run(probe())
    if not tei:
        pytest.skip(f"真实 TEI 不可达（tei={tei}）")
    yield True


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def env(
    engines_available: bool, migrated_database: None,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> AsyncIterator[KbEnv]:
    async with kb_env(
        monkeypatch, tmp_path,
        overrides={"QDRANT_URL": "http://127.0.0.1:56333", "DEV_SKIP_FILE_SCAN": "true"},
    ) as value:
        yield value


async def _chain_to_published(env: KbEnv, monkeypatch: pytest.MonkeyPatch) -> UUID:
    from app.main import create_app
    patch_ports(monkeypatch, FakeParser(ParsedDocument(markdown=MARKDOWN)))
    chain_app = create_app()
    client = AsyncClient(transport=ASGITransport(app=chain_app), base_url="http://test")
    uploader_headers = await login(client, UPLOADER)
    uploaded = await upload(
        client, uploader_headers,
        key=f"bsm4s-{uuid4().hex[:16]}",
        files=[("bsm4s.md", MARKDOWN.encode("utf-8"), "text/markdown")],
        metadata='[{"title": "财务报销手册", "tags": []}]',
    )
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()["documents"][0]
    assert document["status"] == "PENDING_APPROVAL"
    version_id = UUID(document["current_version"]["id"])
    submitted = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/submit-approval",
        headers=uploader_headers,
    )
    assert submitted.status_code == 201, submitted.text
    request_id = submitted.json()["request_id"]
    approver_headers = await login(client, KB_APPROVER)
    approved = await client.post(
        f"/api/v1/school/approvals/{request_id}/approve", headers=approver_headers, json={},
    )
    assert approved.status_code == 200, approved.text
    await configure_audiences(env.tenant_a, version_id, audiences=(("all", None),))
    built = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/build",
        headers=await login(client, KB_BUILDER),
    )
    assert built.status_code == 201, built.text
    result = await build_kb_version({}, str(env.tenant_a), str(version_id))
    assert result["status"] == "ready", result
    from tests.support.kb_env import execute
    await execute("update kb_document_versions set status = 'TEST_PASSED' where id = :id", id=str(version_id))
    await execute(
        "update kb_documents set status = 'TEST_PASSED' where id = :doc and current_version_id = :version",
        doc=str(UUID(document["id"])), version=str(version_id),
    )
    publisher_headers = await login(client, KB_PUBLISHER)
    published = await client.post(
        f"/api/v1/school/kb/versions/{version_id}/publish",
        headers=publisher_headers, json={},
    )
    assert published.status_code == 200, published.text
    await client.aclose()
    from app.auth.revocation import reset_client
    await reset_client()
    return version_id


async def _student_client(env: KbEnv) -> AsyncClient:
    from app.main import create_app
    from app.rbac import sync_rbac_definitions

    admin_engine = make_test_engine("admin")
    try:
        _tenant, user_id = await seed_tenant_user(
            admin_engine, school_code="SCH-KB-A",
            login_id=STUDENT_LOGIN, password=PASSWORD, user_type="student",
        )
        factory = make_session_factory(admin_engine)
        async with factory() as session:
            await sync_rbac_definitions(session)
            await session.commit()
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "insert into user_roles (tenant_id, user_id, role_id) "
                    "select :tenant_id, :user_id, r.id from roles r "
                    "where r.tenant_id = :tenant_id and r.code = 'student'"
                ),
                {"tenant_id": str(env.tenant_a), "user_id": str(user_id)},
            )
        app = create_app()
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    finally:
        await admin_engine.dispose()



def parse_sse(text_body: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    current: str | None = None
    for line in text_body.splitlines():
        if line.startswith("event:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current is not None:
            events.append((current, json.loads(line.split(":", 1)[1])))
            current = None
    return events


async def test_bs_m4_05_multi_turn_persistence_and_clear(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多轮对话：发消息→查历史→上下文注入→清空→后续轮不带入已清空消息。"""

    version_id = await _chain_to_published(env, monkeypatch)
    student_client = await _student_client(env)

    fake = FakeLlm(
        complete_scripts=[
            LlmResponse(
                text=json.dumps({"intents": [{"name": "kb_qa", "query": "差旅报销怎么办理？"}]}),
                finish_reason="stop",
            ),
            LlmResponse(
                text=json.dumps({"intents": [{"name": "kb_qa", "query": "招待报销呢？"}]}),
                finish_reason="stop",
            ),
        ],
        stream_scripts=[
            [LlmStreamEvent(kind="delta", text_delta="差旅报销需要发票和审批单 [1]。"),
             LlmStreamEvent(kind="finish", finish_reason="stop")],
            [LlmStreamEvent(kind="delta", text_delta="招待费需注明事由 [1]。"),
             LlmStreamEvent(kind="finish", finish_reason="stop")],
        ],
    )
    from app.harness.trace_db import DbTraceSink
    sink = DbTraceSink()
    settings = get_settings()
    loop = build_chat_loop(settings, trace_sink=sink, llm_port=fake)
    from app.main import create_app
    app = create_app(chat_loop=loop)
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    conversation_id = str(uuid4())

    # 不用 async with——手动管理生命周期，避免 httpx client 二次进入
    await client.__aenter__()
    await student_client.__aenter__()
    try:
        headers = await login(client, STUDENT_LOGIN)

        # 第 1 轮
        resp1 = await client.post(
            "/api/v1/campus/chat/turns",
            json={"message": "差旅报销怎么办理？", "conversation_id": conversation_id},
            headers=headers,
        )
        assert resp1.status_code == 200, resp1.text
        events1 = parse_sse(resp1.text)
        assert events1[-1][1]["stop_reason"] == "FINAL_ANSWER"
        conv_id = events1[0][1]["conversation_id"]

        # 第 2 轮（同会话）
        resp2 = await client.post(
            "/api/v1/campus/chat/turns",
            json={"message": "招待报销呢？", "conversation_id": conv_id},
            headers=headers,
        )
        assert resp2.status_code == 200, resp2.text
        events2 = parse_sse(resp2.text)
        assert events2[-1][1]["stop_reason"] == "FINAL_ANSWER"

        # 上下文注入验证（第 2 轮审查 Major-3：断言必须能区分「注入了历史」与「没注入」）
        # 末位消息 = 合成 prompt 模板（系统词+参考资料+本轮问题）；历史逐条 verbatim 注入在其前
        generate_messages = fake.requests[-1].messages
        assert "招待报销呢？" in generate_messages[-1].content
        first_round = [
            m for m in generate_messages[:-1]
            if m.role == "user" and m.content == "差旅报销怎么办理？"
        ]
        assert first_round, "第一轮 user 原文未注入 LLM 上下文（AI-06 卡 3-A 失效）"

        # 消息持久化验证：4 条（2 轮 × user+assistant）
        msg_rows = await rows(
            "select count(*) as n from conversation_messages "
            "where conversation_id = :cid and status != 'deleted'",
            cid=conv_id,
        )
        assert int(msg_rows[0]["n"]) == 4

        # A4：首轮创建时 pinned_kb_version 已写入当前活跃发布版本
        conv_rows = await rows(
            "select pinned_kb_version_id from conversations where id = :cid",
            cid=conv_id,
        )
        assert conv_rows[0]["pinned_kb_version_id"] is not None
        assert str(conv_rows[0]["pinned_kb_version_id"]) == str(version_id)

        # A5/A6：每回合 trace + usage 各一条（provider 落 llm_usage；chat_traces 无 provider 列）
        trace_rows = await rows(
            "select route_path, model, stop_reason from chat_traces "
            "where conversation_id = :cid order by created_at",
            cid=conv_id,
        )
        assert len(trace_rows) == 2
        assert all(t["route_path"] == "fast" for t in trace_rows)
        assert all(t["model"] == "fake-model" for t in trace_rows)
        assert all(t["stop_reason"] == "FINAL_ANSWER" for t in trace_rows)
        usage_rows = await rows(
            "select provider, model from llm_usage where tenant_id = :t",
            t=str(env.tenant_a),
        )
        assert len(usage_rows) == 2
        assert all(u["provider"] == "fake" for u in usage_rows)

        # AI-07 清空
        clear_resp = await client.delete(
            f"/api/v1/campus/conversations/{conv_id}/messages",
            headers=headers,
        )
        assert clear_resp.status_code == 200, clear_resp.text
        assert clear_resp.json()["cleared"] == 4

        # 清空后查历史：空
        history_resp = await client.get(
            f"/api/v1/campus/conversations/{conv_id}/messages",
            headers=headers,
        )
        assert history_resp.status_code == 200
        assert history_resp.json()["messages"] == []

        # 幂等
        clear_resp2 = await client.delete(
            f"/api/v1/campus/conversations/{conv_id}/messages",
            headers=headers,
        )
        assert clear_resp2.status_code == 200
        assert clear_resp2.json()["cleared"] == 0

        # 审计验证
        audit_rows = await rows(
            "select count(*) as n from audit_logs "
            "where tenant_id = :t and action = 'campus.conversation.clear'",
            t=str(env.tenant_a),
        )
        assert int(audit_rows[0]["n"]) >= 1
    finally:
        await client.aclose()
        await student_client.aclose()
