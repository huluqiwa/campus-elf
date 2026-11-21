"""BS-M4-03：ReAct 多意图对话业务场景（M4-AI01c；真实 PG+Qdrant+TEI + FakeLlm）。

Given 已发布知识库（财务报销手册）+ 学生身份；When 多意图提问
（知识库问答 + 入口查询）；Then 路由→ReAct→合并作答全链：分段小节、
引用全局编号（KB 文档 + 业务入口）、navigate 工具真实执行、trace route_path=react。
MinerU 不需要（MD + FakeParser）；TEI 不可达即整模块 skip（CI 无容器）。
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
from app.harness.trace import InMemoryTraceSink
from app.kb.build_task import build_kb_version
from app.kb.parser import ParsedDocument
from tests.support.auth import seed_tenant_user
from tests.support.db import (
    make_session_factory,
    make_test_engine,
    upgrade_test_database,
)
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

差旅报销需要发票和审批单。报销流程第一步提交申请。第二步部门负责人审批。第三步财务复核打款。

## 第二章 招待报销

招待费报销需注明事由。招待标准按级别执行。超额部分不予报销。
"""

STUDENT_LOGIN = "bs-m4-react-student"


@pytest.fixture(scope="module")
def engines_available() -> Iterator[bool]:
    """真实 TEI 探测：不可达即整模块 skip（CI 无容器；本地 dev 栈常开）。"""

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
        pytest.skip(f"真实 TEI 不可达（tei={tei}）；本用例只在本地 dev 栈跑")
    yield True


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def env(
    engines_available: bool,
    migrated_database: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[KbEnv]:
    async with kb_env(
        monkeypatch,
        tmp_path,
        overrides={
            "QDRANT_URL": "http://127.0.0.1:56333",
            # 扫描跳过仅测试编排（KB-01C/e2e 先例；生产校验红线不变）
            "DEV_SKIP_FILE_SCAN": "true",
        },
    ) as value:
        yield value


async def _chain_to_published(env: KbEnv, monkeypatch: pytest.MonkeyPatch) -> UUID:
    """上传 → 审批 → 人群配置 → 真实构建 → TEST_PASSED → 发布（HTTP 全链）。"""

    from app.main import create_app

    patch_ports(monkeypatch, FakeParser(ParsedDocument(markdown=MARKDOWN)))
    chain_app = create_app()
    client = AsyncClient(transport=ASGITransport(app=chain_app), base_url="http://test")
    uploader_headers = await login(client, UPLOADER)
    uploaded = await upload(
        client,
        uploader_headers,
        key=f"bsm4c-{uuid4().hex[:16]}",
        files=[("bsm4c.md", MARKDOWN.encode("utf-8"), "text/markdown")],
        metadata='[{"title": "财务报销手册", "tags": []}]',
    )
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()["documents"][0]
    assert document["status"] == "PENDING_APPROVAL"
    doc_id = UUID(document["id"])
    version_id = UUID(document["current_version"]["id"])

    submitted = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/submit-approval",
        headers=uploader_headers,
    )
    assert submitted.status_code == 201, submitted.text
    request_id = submitted.json()["request_id"]
    approver_headers = await login(client, KB_APPROVER)
    approved = await client.post(
        f"/api/v1/school/approvals/{request_id}/approve",
        headers=approver_headers,
        json={},
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

    await execute(
        "update kb_document_versions set status = 'TEST_PASSED' where id = :id",
        id=str(version_id),
    )
    await execute(
        "update kb_documents set status = 'TEST_PASSED' "
        "where id = :doc and current_version_id = :version",
        doc=str(doc_id),
        version=str(version_id),
    )
    publisher_headers = await login(client, KB_PUBLISHER)
    published = await client.post(
        f"/api/v1/school/kb/versions/{version_id}/publish",
        headers=publisher_headers,
        json={},
    )
    assert published.status_code == 200, published.text
    await client.aclose()
    return version_id


async def _student_client(env: KbEnv) -> AsyncClient:
    """学生 ASGI 客户端（出厂矩阵真实生效）。"""

    from app.auth.revocation import reset_client
    from app.main import create_app
    from app.rbac import sync_rbac_definitions

    admin_engine = make_test_engine("admin")
    try:
        _tenant, user_id = await seed_tenant_user(
            admin_engine,
            school_code="SCH-KB-A",
            login_id=STUDENT_LOGIN,
            password=PASSWORD,
            user_type="student",
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
        await reset_client()


def _chat_client(env: KbEnv, fake_llm: Any) -> tuple[AsyncClient, InMemoryTraceSink]:
    """对话客户端：真实检索装配 + FakeLlm（路由/THINK/GENERATE 脚本化）。"""

    from app.main import create_app

    sink = InMemoryTraceSink()
    settings = get_settings()
    loop = build_chat_loop(settings, trace_sink=sink, llm_port=fake_llm)
    app = create_app(chat_loop=loop)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test"), sink


def parse_sse(text_body: str) -> list[tuple[str, dict[str, Any]]]:
    """SSE 文本 → [(event, data)]。"""

    events: list[tuple[str, dict[str, Any]]] = []
    current: str | None = None
    for line in text_body.splitlines():
        if line.startswith("event:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current is not None:
            events.append((current, json.loads(line.split(":", 1)[1])))
            current = None
    return events


async def test_bs_m4_03_multi_intent_react_full_stream(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given 已发布 KB；When 多意图提问；Then 路由→ReAct→合并作答（分段+全局引用+入口）。"""

    await _chain_to_published(env, monkeypatch)
    student_client = await _student_client(env)
    fake = FakeLlm(
        complete_scripts=[
            # 路由（orchestrator）
            LlmResponse(
                text=json.dumps(
                    {
                        "intents": [
                            {"name": "kb_qa", "query": "差旅报销怎么办理？"},
                            {"name": "navigate", "query": "请假入口在哪？"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                finish_reason="stop",
            ),
            # THINK 1：检索
            LlmResponse(
                text="",
                finish_reason="tool_calls",
                tool_calls=(
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "kb.search",
                            "arguments": json.dumps(
                                {"query": "差旅报销怎么办理？"}, ensure_ascii=False
                            ),
                        },
                    },
                ),
            ),
            # THINK 2：入口查询
            LlmResponse(
                text="",
                finish_reason="tool_calls",
                tool_calls=(
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {
                            "name": "campus.navigate",
                            "arguments": json.dumps({"target": "请假"}, ensure_ascii=False),
                        },
                    },
                ),
            ),
            # THINK 3：资料足够
            LlmResponse(text="DONE", finish_reason="stop"),
        ],
        stream_scripts=[
            [
                LlmStreamEvent(
                    kind="delta",
                    # AI-04：citation=被引子集——每个 [n] 的句子必须与被引块语义对齐
                    # （[1]=差旅块 [2]=招待块 [3]=nav:请假 入口豁免通道；第 1 轮审查
                    # Major-1：错位引用曾以 0.0098 余量漏过阈值，勿回退）
                    text_delta="差旅报销需要发票和审批单 [1]。招待费需注明事由 [2]。请假办理入口 [3]。",
                ),
                LlmStreamEvent(kind="finish", finish_reason="stop"),
                LlmStreamEvent(kind="usage", tokens_in=300, tokens_out=30),
            ]
        ],
    )
    client, sink = _chat_client(env, fake)
    async with client as ac, student_client as sc:
        headers = await login(sc, STUDENT_LOGIN)
        response = await ac.post(
            "/api/v1/campus/chat/turns",
            json={"message": "差旅报销怎么办理？另外请假入口在哪？"},
            headers=headers,
        )
    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    names = [name for name, _ in events]

    assert names[0] == "meta"
    assert names.count("meta") == 1
    assert names[-1] == "done"
    # ReAct 循环可见：think 步 ×3、两个工具各一次 tool_call/tool_result
    think_steps = [
        data for name, data in events if name == "step" and data.get("stage") == "think"
    ]
    assert len(think_steps) == 3
    tool_calls = [data for name, data in events if name == "tool_call"]
    assert [data["tool"] for data in tool_calls] == ["kb.search", "campus.navigate"]
    tool_results = [data for name, data in events if name == "tool_result"]
    assert all(data["status"] == "ok" for data in tool_results)
    # GENERATE：分段回答 + 全局引用（KB 文档 + 业务入口）
    summaries = [data.get("summary", "") for name, data in events if name == "reasoning_summary"]
    assert any("识别到 2 个问题" in summary for summary in summaries)
    citations = [data for name, data in events if name == "citation"]
    assert [data["index"] for data in citations] == [1, 2, 3]  # KB 两子块 + 业务入口（全局编号）
    assert citations[0]["document"] == "财务报销手册"
    assert citations[-1]["document"] == "业务入口"
    assert citations[-1]["section"] is None  # 入口证据无章节
    done = events[-1][1]
    assert done["stop_reason"] == "FINAL_ANSWER"
    assert done["tokens_in"] == 300
    # trace：route_path=react + intents + budget span
    assert sink.turns[-1].route_path == "react"
    assert sink.turns[-1].intents == ["kb_qa", "navigate"]
    budget_spans = [span for span in sink.spans if span.span_type == "budget"]
    assert budget_spans and budget_spans[-1].attributes["tool_calls"] == 2
    # 业务输出：审计表真实落库（两次工具执行）
    audit_rows = await rows(
        "select count(*) as n from audit_logs where tenant_id = :t and action = 'tool.execute'",
        t=str(env.tenant_a),
    )
    assert audit_rows[0]["n"] >= 2
