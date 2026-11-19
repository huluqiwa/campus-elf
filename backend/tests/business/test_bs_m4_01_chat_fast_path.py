"""BS-M4-01/02：C 端对话快路径业务场景（M4-AI01b）。

Given 真实 PG+Qdrant+TEI（上传→审批→人群配置→真实构建→TEST_PASSED→发布）；
FakeLlm 注入（真实 provider 缺位——TD-327）；HTTP 全链 + SSE 解析。
断言业务输出：SSE 事件序、引用与真实 payload 同源、FakeLlm 收到的证据文本、
审计表落库、trace 终态；ACL fail-closed（无权零召回零引用零标题泄漏）、
LLM 未配置兜底。

MinerU 不需要（MD 直规范化 + FakeParser）；TEI 不可达即整模块 skip（CI 无容器）。
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
from tests.support.kb_publish_seed import (
    FakeParser,
    patch_ports,
)
from tests.support.llm import FakeLlm

pytestmark = [pytest.mark.integration]

MARKDOWN = """# 财务报销手册

## 第一章 差旅报销

差旅报销需要发票和审批单。报销流程第一步提交申请。第二步部门负责人审批。第三步财务复核打款。

## 第二章 招待报销

招待费报销需注明事由。招待标准按级别执行。超额部分不予报销。
"""

STUDENT_LOGIN = "bs-m4-student"
CLASS_AUDIENCE_ID = "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f"  # 学生不在该班（fail-closed 面）


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
            # 扫描跳过仅测试编排（KB-01C/e2e 先例；生产校验红线不变）——CI 无 ClamAV 也能走通链
            "DEV_SKIP_FILE_SCAN": "true",
        },
    ) as value:
        yield value


# ---------------------------------------------------------------------------
# 链路助手：HTTP 全链到 TEST_PASSED → 发布 → 学生身份 + 带 FakeLlm 的对话客户端
# ---------------------------------------------------------------------------


async def _chain_to_published(
    env: KbEnv,
    monkeypatch: pytest.MonkeyPatch,
    *,
    audiences: tuple[tuple[str, str | None], ...],
) -> UUID:
    """上传 → 提交审批 → 通过 → 配人群 → 真实构建 READY → TEST_PASSED → 发布。"""

    from app.main import create_app

    patch_ports(monkeypatch, FakeParser(ParsedDocument(markdown=MARKDOWN)))
    chain_app = create_app()
    client = AsyncClient(
        transport=ASGITransport(app=chain_app), base_url="http://test"
    )
    uploader_headers = await login(client, UPLOADER)
    uploaded = await upload(
        client,
        uploader_headers,
        key=f"bsm4-{uuid4().hex[:16]}",
        files=[("bsm4.md", MARKDOWN.encode("utf-8"), "text/markdown")],
        metadata='[{"title": "财务报销手册", "tags": []}]',
    )
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()["documents"][0]
    assert document["status"] == "PENDING_APPROVAL"  # DEV_SKIP_FILE_SCAN=true 直置待审
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

    await configure_audiences(env.tenant_a, version_id, audiences=audiences)
    built = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/build",
        headers=await login(client, KB_BUILDER),
    )
    assert built.status_code == 201, built.text
    result = await build_kb_version({}, str(env.tenant_a), str(version_id))
    assert result["status"] == "ready", result
    await _execute(
        "update kb_document_versions set status = 'TEST_PASSED' where id = :id",
        id=str(version_id),
    )
    await _execute(
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


async def _execute(sql: str, **params: object) -> None:
    from tests.support.kb_env import execute

    await execute(sql, **params)


def _chat_client(env: KbEnv, fake_llm: FakeLlm | None) -> tuple[AsyncClient, InMemoryTraceSink]:
    """学生对话客户端：真实检索装配 + FakeLlm 注入 + 内存 trace sink。"""

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


# ---------------------------------------------------------------------------
# BS-M4-01：全链发布后对话（真实检索 + FakeLlm）
# ---------------------------------------------------------------------------


async def test_bs_m4_01_published_kb_chat_full_stream(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given 已发布知识库；When 学生提问命中内容；Then 事件序完整、引用同源、审计落库。"""

    await _chain_to_published(env, monkeypatch, audiences=(("all", None),))
    student_client, _ = await _student_client(env)
    fake = FakeLlm(
        complete_scripts=[
            # orchestrator 路由（单 kb_qa → simple → 快路径）
            LlmResponse(
                text=json.dumps(
                    {"intents": [{"name": "kb_qa", "query": "差旅报销怎么办理？"}]},
                    ensure_ascii=False,
                ),
                finish_reason="stop",
            ),
        ],
        stream_scripts=[
            [
                LlmStreamEvent(kind="delta", text_delta="差旅报销需要发票和审批单 [1]。"),
                LlmStreamEvent(kind="finish", finish_reason="stop"),
                LlmStreamEvent(kind="usage", tokens_in=150, tokens_out=12),
            ]
        ]
    )
    client, sink = _chat_client(env, fake)
    async with client as ac, student_client as sc:
        headers = await login(sc, STUDENT_LOGIN)
        response = await ac.post(
            "/api/v1/campus/chat/turns",
            json={"message": "差旅报销怎么办理？"},
            headers=headers,
        )
    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names == [
        "meta",
        "step",
        "step",
        "tool_call",
        "tool_result",
        "reasoning_summary",
        "delta",
        "citation",
        "citation",
        "done",
    ]  # 两个子块都命中（MARKDOWN 两章节各一块）
    tool_result = events[4][1]
    assert tool_result["status"] == "ok"
    assert tool_result["hits"] == 2
    citations = [data for name, data in events if name == "citation"]
    assert [item["index"] for item in citations] == [1, 2]
    assert citations[0]["document"] == "财务报销手册"
    assert any("差旅报销" in item["section"] for item in citations)
    # FakeLlm 收到的上下文含真实证据文本（GENERATE 消费 RETRIEVE 结果）
    generate_prompt = fake.requests[-1].messages[0].content
    assert "差旅报销需要发票和审批单" in generate_prompt
    assert "[1]" in generate_prompt
    done = events[-1][1]
    assert done["stop_reason"] == "FINAL_ANSWER"
    assert done["tokens_in"] == 150
    # 业务输出：审计表真实落库（tool.execute）
    audit_rows = await rows(
        "select count(*) as n from audit_logs where tenant_id = :t and action = 'tool.execute'",
        t=str(env.tenant_a),
    )
    assert audit_rows[0]["n"] >= 1
    # trace 终态
    assert sink.turns[-1].stop_reason is not None
    assert sink.turns[-1].stop_reason.value == "FINAL_ANSWER"


async def _student_client(env: KbEnv) -> tuple[AsyncClient, UUID]:
    """学生 ASGI 客户端：学生落在 env.tenant_a（SCH-KB-A），出厂矩阵真实生效。"""

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
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        return client, user_id
    finally:
        await admin_engine.dispose()
        await reset_client()


# ---------------------------------------------------------------------------
# BS-M4-02：ACL fail-closed 零泄漏 + LLM 未配置兜底
# ---------------------------------------------------------------------------


async def test_bs_m4_02_acl_fail_closed_no_leak(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given 文档人群=学生不在的班级；When 学生提问；Then 零召回零引用零标题泄漏。"""

    await _chain_to_published(
        env, monkeypatch, audiences=(("class", CLASS_AUDIENCE_ID),)
    )
    student_client, _ = await _student_client(env)
    fake = FakeLlm(
        complete_scripts=[
            LlmResponse(
                text=json.dumps(
                    {"intents": [{"name": "kb_qa", "query": "差旅报销怎么办理？"}]},
                    ensure_ascii=False,
                ),
                finish_reason="stop",
            ),
        ],
    )
    client, _sink = _chat_client(env, fake)
    async with client as ac, student_client as sc:
        headers = await login(sc, STUDENT_LOGIN)
        response = await ac.post(
            "/api/v1/campus/chat/turns",
            json={"message": "差旅报销怎么办理？"},
            headers=headers,
        )
    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names == [
        "meta",
        "step",
        "step",
        "tool_call",
        "tool_result",
        "reasoning_summary",
        "delta",
        "done",
    ]
    assert events[4][1]["hits"] == 0  # ACL filter 真实生效（零召回）
    delta = events[6][1]
    assert delta["text"] == "抱歉，我暂时没有在学校知识库中找到与这个问题相关的内容。你可以换个说法再试一次，或联系管理员确认相关资料是否已发布。"
    # 零泄漏：整条流不出现文档标题/章节/正文片段
    assert "财务报销手册" not in response.text
    assert "差旅报销需要发票" not in response.text
    # orchestrator 化后路由触达 LLM 属正常；零命中下仅路由一次、无 GENERATE 流
    assert len(fake.requests) == 1
    assert "意图分类器" in fake.requests[0].messages[0].content
    assert events[7][1]["stop_reason"] == "CAPABILITY_FALLBACK"


async def test_bs_m4_02_llm_not_configured_fallback(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given 已发布知识库 + LLM 未配置；When 学生提问；Then error(LLM_NOT_CONFIGURED) 无 done。"""

    await _chain_to_published(env, monkeypatch, audiences=(("all", None),))
    student_client, _ = await _student_client(env)
    client, sink = _chat_client(env, None)  # llm_port=None（未配置）
    async with client as ac, student_client as sc:
        headers = await login(sc, STUDENT_LOGIN)
        response = await ac.post(
            "/api/v1/campus/chat/turns",
            json={"message": "差旅报销怎么办理？"},
            headers=headers,
        )
    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[-1] == "error"
    assert events[-1][1]["code"] == "LLM_NOT_CONFIGURED"
    assert events[-1][1]["retryable"] is False
    assert all(name != "done" for name, _ in events)
    assert all("财务报销手册" not in data.get("text", "") for _, data in events)
    assert sink.turns[-1].stop_reason is not None
    assert sink.turns[-1].stop_reason.value == "CAPABILITY_FALLBACK"
