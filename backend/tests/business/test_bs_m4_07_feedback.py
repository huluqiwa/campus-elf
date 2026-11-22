"""BS-M4-07：反馈采集业务场景（M4-AI05；真实 PG+Qdrant+TEI + FakeLlm）。

Given 已发布知识库 + 学生提问 + 校方处理者；When 全链走一遍；Then：
1. AI 回合后提交反馈（201；consent_at/快照密文/审计落库）
2. B 端详情受控解密（问题/答案/引用明文）→ 标记处理（幂等）→ 答复落表（审计）
3. 学生自助删除 → 匿名化（身份断链/快照清空/审计）→ B 端详情不再展示快照
MinerU 不需要；TEI 不可达即整模块 skip（CI 无容器）。
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import get_settings
from app.harness.chat_assembly import build_chat_loop
from app.harness.llm import LlmResponse, LlmStreamEvent
from app.harness.trace_db import DbTraceSink
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

差旅报销需要发票和审批单。报销流程第一步提交申请。

## 第二章 招待报销

招待费报销需注明事由。招待标准按级别执行。
"""

STUDENT_LOGIN = "bs-m4-fb-student"
HANDLER_LOGIN = "bs-m4-fb-handler"


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
    from app.auth.revocation import reset_client
    from app.main import create_app
    patch_ports(monkeypatch, FakeParser(ParsedDocument(markdown=MARKDOWN)))
    chain_app = create_app()
    client = AsyncClient(transport=ASGITransport(app=chain_app), base_url="http://test")
    uploader_headers = await login(client, UPLOADER)
    uploaded = await upload(
        client, uploader_headers,
        key=f"bsm4f-{uuid4().hex[:16]}",
        files=[("bsm4f.md", MARKDOWN.encode("utf-8"), "text/markdown")],
        metadata='[{"title": "财务报销手册", "tags": []}]',
    )
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()["documents"][0]
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
    await reset_client()
    return version_id


async def _clients(env: KbEnv) -> tuple[AsyncClient, AsyncClient]:
    """学生（带 chat loop）+ 校方处理者 两个客户端。"""

    from app.auth.revocation import reset_client
    from app.db.session import dispose_engine
    from app.main import create_app
    from app.rbac import sync_rbac_definitions

    admin_engine = make_test_engine("admin")
    try:
        _tenant, user_id = await seed_tenant_user(
            admin_engine, school_code="SCH-KB-A",
            login_id=STUDENT_LOGIN, password=PASSWORD, user_type="student",
        )
        _tenant2, _handler_id = await seed_tenant_user(
            admin_engine, school_code="SCH-KB-A",
            login_id=HANDLER_LOGIN, password=PASSWORD, user_type="teacher",
            permissions=("school.feedback.view", "school.feedback.update"),
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
    finally:
        await admin_engine.dispose()

    fake = FakeLlm(
        complete_scripts=[
            LlmResponse(
                text=json.dumps({"intents": [{"name": "kb_qa", "query": "差旅报销怎么办理？"}]}),
                finish_reason="stop",
            ),
        ],
        stream_scripts=[
            [LlmStreamEvent(kind="delta", text_delta="差旅报销需要发票和审批单 [1]。"),
             LlmStreamEvent(kind="finish", finish_reason="stop"),
             LlmStreamEvent(kind="usage", tokens_in=150, tokens_out=30)],
        ],
    )
    loop = build_chat_loop(get_settings(), trace_sink=DbTraceSink(), llm_port=fake)
    chat_app = create_app(chat_loop=loop)
    student_client = AsyncClient(transport=ASGITransport(app=chat_app), base_url="http://test")
    admin_client = AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")
    await reset_client()
    _ = dispose_engine
    return student_client, admin_client


def parse_sse(text_body: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    current: str | None = None
    for line in text_body.splitlines():
        if line.startswith("event:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current is not None:
            events.append((current, json.loads(line.split(":", 1)[1])))
            current = None
    return events


@pytest.mark.asyncio
async def test_bs_m4_07_feedback_full_chain(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全链：AI 回合 → 提交 → B 端处理/答复 → 自助删除匿名化。"""

    await _chain_to_published(env, monkeypatch)
    student_client, admin_client = await _clients(env)
    await student_client.__aenter__()
    await admin_client.__aenter__()
    try:
        student_headers = await login(student_client, STUDENT_LOGIN)
        handler_headers = await login(admin_client, HANDLER_LOGIN)

        # 1) AI 回合（真实栈：检索 + 流式）
        resp = await student_client.post(
            "/api/v1/campus/chat/turns",
            json={"message": "差旅报销怎么办理？"},
            headers=student_headers,
        )
        assert resp.status_code == 200, resp.text
        events = parse_sse(resp.text)
        meta = events[0][1]
        assert events[-1][1].get("stop_reason") == "FINAL_ANSWER", events[-1]
        conversation_id = str(meta["conversation_id"])
        turn_id = str(meta["turn_id"])
        trace_id = str(meta["trace_id"])

        # 2) 提交反馈（201；审计落库）
        submitted = await student_client.post(
            "/api/v1/campus/feedback",
            json={
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "trace_id": trace_id,
                "rating": "unhelpful",
                "reason_tags": ["inaccurate"],
                "comment": "引用的条目和审批流程对不上。",
            },
            headers=student_headers,
        )
        assert submitted.status_code == 201, submitted.text
        feedback_id = submitted.json()["feedback_id"]
        audit_rows = await rows(
            "select count(*) as n from audit_logs where tenant_id = :t "
            "and action = 'campus.feedback.create'",
            t=str(env.tenant_a),
        )
        assert int(audit_rows[0]["n"]) == 1

        # 3) B 端详情：受控解密（真实问答明文）
        detail = await admin_client.get(
            f"/api/v1/school/feedback/{feedback_id}", headers=handler_headers,
        )
        assert detail.status_code == 200, detail.text
        detail_body = detail.json()
        assert detail_body["snapshot"]["question"] == "差旅报销怎么办理？"
        assert "差旅报销需要发票和审批单" in detail_body["snapshot"]["answer"]

        # 4) 标记处理（幂等）+ 答复（审计）
        handled = await admin_client.post(
            f"/api/v1/school/feedback/{feedback_id}/handle", headers=handler_headers,
        )
        assert handled.status_code == 200
        assert handled.json()["executed_now"] is True
        reply = await admin_client.post(
            f"/api/v1/school/feedback/{feedback_id}/reply",
            json={"content": "已核实，知识库会更新。"}, headers=handler_headers,
        )
        assert reply.status_code == 200, reply.text
        handled_audit = await rows(
            "select count(*) as n from audit_logs where tenant_id = :t "
            "and action in ('school.feedback.handled', 'school.feedback.replied')",
            t=str(env.tenant_a),
        )
        assert int(handled_audit[0]["n"]) == 2

        # 5) 学生自助删除 → 匿名化 → B 端详情不再展示快照（A7）
        deleted = await student_client.delete(
            f"/api/v1/campus/feedback/{feedback_id}", headers=student_headers,
        )
        assert deleted.status_code == 200, deleted.text
        detail_after = await admin_client.get(
            f"/api/v1/school/feedback/{feedback_id}", headers=handler_headers,
        )
        assert detail_after.status_code == 200
        after_body = detail_after.json()
        assert after_body["anonymized"] is True
        assert after_body["snapshot"] is None
        assert after_body["comment"] is None
        assert after_body["rating"] == "unhelpful"  # 统计面保留
    finally:
        await student_client.aclose()
        await admin_client.aclose()
