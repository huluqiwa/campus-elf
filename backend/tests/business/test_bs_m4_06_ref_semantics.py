"""BS-M4-06：引用语义校验业务场景（M4-AI04；真实 PG+Qdrant+TEI + FakeLlm）。

Given 已发布知识库（差旅/招待两章节）+ 学生身份 + 真实 TEI 语义校验；When 多轮提问；Then：
1. 有效引用全保留（答案逐句对应被引块 → sim ≥ 0.7 → citation 齐全、零 ref_irrelevant 事件）
2. 幻觉引用剔除+留痕（答案引用 [2] 但内容与之无关 → sim < 0.7 → citation 过滤
   + guard_events 行 output.ref_irrelevant + verify span 落库）
3. 全部引用失效（答案与两个块都无关 → 零 citation 事件 + 两条 guard 行；零证据兜底回归由 BS-M4-02 ACL 弧背书）
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

STUDENT_LOGIN = "bs-m4-refsem-student"


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
        key=f"bsm4r-{uuid4().hex[:16]}",
        files=[("bsm4r.md", MARKDOWN.encode("utf-8"), "text/markdown")],
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
async def test_bs_m4_06_ref_semantics_arcs(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三弧：有效全保留 / 幻觉剔除+留痕 / 零证据兜底（真实 TEI 校验）。"""

    await _chain_to_published(env, monkeypatch)
    student_client = await _student_client(env)

    fake = FakeLlm(
        complete_scripts=[
            LlmResponse(
                text=json.dumps({"intents": [{"name": "kb_qa", "query": "差旅报销怎么办理？"}]}),
                finish_reason="stop",
            ),
            LlmResponse(
                text=json.dumps({"intents": [{"name": "kb_qa", "query": "差旅报销流程"}]}),
                finish_reason="stop",
            ),
            LlmResponse(
                text=json.dumps({"intents": [{"name": "kb_qa", "query": "量子力学期末考试范围"}]}),
                finish_reason="stop",
            ),
        ],
        stream_scripts=[
            # 弧 1：答案逐句对应两个被引块（真实 TEI sim ≈ 0.82/0.84 ≥ 0.7 → 全保留）
            [LlmStreamEvent(kind="delta", text_delta="差旅报销需要发票和审批单 [1]。招待费需注明事由 [2]。"),
             LlmStreamEvent(kind="finish", finish_reason="stop"),
             LlmStreamEvent(kind="usage", tokens_in=150, tokens_out=30)],
            # 弧 2：[1] 对应差旅块（近原文 → 保留）；[2] 引招待块但答案是天气内容
            # （真实 TEI sim ≈ 0.56 < 0.7 → 剔除）
            [LlmStreamEvent(kind="delta", text_delta="差旅报销需要发票和审批单，报销流程第一步提交申请 [1]。今天天气晴朗适合户外运动 [2]。"),
             LlmStreamEvent(kind="finish", finish_reason="stop"),
             LlmStreamEvent(kind="usage", tokens_in=160, tokens_out=32)],
            # 弧 3：两个引用都与答案无关（纯天气文本 vs 报销语料 sim ≈ 0.5x）→ 全剔除
            [LlmStreamEvent(kind="delta", text_delta="今天天气晴朗适合户外运动 [1]。运动会出汗记得补水 [2]。"),
             LlmStreamEvent(kind="finish", finish_reason="stop"),
             LlmStreamEvent(kind="usage", tokens_in=140, tokens_out=28)],
        ],
    )
    from app.harness.trace_db import DbTraceSink
    sink = DbTraceSink()
    settings = get_settings()
    loop = build_chat_loop(settings, trace_sink=sink, llm_port=fake)
    from app.main import create_app
    app = create_app(chat_loop=loop)
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    await client.__aenter__()
    await student_client.__aenter__()
    try:
        headers = await login(client, STUDENT_LOGIN)

        async def turn(message: str) -> tuple[str, str, list[dict[str, object]]]:
            """发一轮回合；返回 (trace_id, stop_reason, citations)。"""

            resp = await client.post(
                "/api/v1/campus/chat/turns", json={"message": message}, headers=headers,
            )
            assert resp.status_code == 200, resp.text
            events = parse_sse(resp.text)
            meta = events[0][1]
            done = events[-1][1]
            citations = [
                dict(data) for name, data in events if name == "citation"
            ]
            return str(meta["trace_id"]), str(done["stop_reason"]), citations

        # 弧 1：有效引用全保留（真实 TEI sim ≈ 0.82/0.84 ≥ 0.7）
        trace1, stop1, citations1 = await turn("差旅报销怎么办理？")
        assert stop1 == "FINAL_ANSWER"
        assert [c["index"] for c in citations1] == [1, 2]
        rows1 = await rows(
            "select count(*) as n from guard_events where trace_id = :t "
            "and rule = 'output.ref_irrelevant'",
            t=trace1,
        )
        assert int(rows1[0]["n"]) == 0

        # 弧 2：幻觉引用剔除 + 留痕（[2] 招待块 vs 天气答案 sim ≈ 0.56 < 0.7）
        trace2, stop2, citations2 = await turn("差旅报销流程")
        assert stop2 == "FINAL_ANSWER"
        assert [c["index"] for c in citations2] == [1]
        removed_rows = await rows(
            "select rule, severity, action, excerpt_redacted from guard_events "
            "where trace_id = :t and rule = 'output.ref_irrelevant'",
            t=trace2,
        )
        assert len(removed_rows) == 1
        assert removed_rows[0]["severity"] == "low"
        assert removed_rows[0]["action"] == "redact"
        assert "[2]" in str(removed_rows[0]["excerpt_redacted"])
        # verify span 落库（A6：removed=1 数值型 attributes）
        span_rows = await rows(
            "select attributes_json::text as attrs from chat_trace_spans "
            "where trace_id = :t and span_type = 'verify'",
            t=trace2,
        )
        assert len(span_rows) == 1
        attrs = json.loads(str(span_rows[0]["attrs"]))
        assert attrs["checked"] == 2
        assert attrs["removed"] == 1

        # 弧 3：全部引用失效 → 零 citation 事件 + 两条 guard 行（A2 边界）
        trace3, stop3, citations3 = await turn("今天天气怎么样？")
        assert stop3 == "FINAL_ANSWER"  # 正文保留（流式已发，不追加兜底）
        assert citations3 == []
        rows3 = await rows(
            "select count(*) as n from guard_events where trace_id = :t "
            "and rule = 'output.ref_irrelevant'",
            t=trace3,
        )
        assert int(rows3[0]["n"]) == 2
    finally:
        await client.aclose()
        await student_client.aclose()
