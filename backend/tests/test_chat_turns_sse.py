"""C 端 chat SSE 端点校验（M4-AI01b 步 5；替换 test_skeleton_loop.py）。

注入 FastPathLoop（FakeLlm + stub retriever）+ dependency_overrides 注入
Principal 走 ASGI 流：SSE 编码/事件序（零命中兜底，离线确定）、RBAC 边界、
装配错误显式失败。真实栈的 RBAC/审计路径由 test_campus_chat_rbac.py 覆盖。
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth.principal import Principal, get_current_principal
from app.harness.loop import CAPABILITY_FALLBACK_TEXT, FastPathLoop
from app.harness.prompt_loading import build_default_registry
from app.harness.trace import InMemoryTraceSink
from app.kb.chat_retrieval import ChatEvidence, ChatRetriever
from app.kb.embedding import TEIEmbedder
from app.main import create_app
from tests.support.llm import FakeLlm


class _StubEmbedder(TEIEmbedder):
    """无网络 embedder（继承真实类以通过类型面）。"""

    def __init__(self) -> None:
        super().__init__(base_url="http://127.0.0.1:1", timeout_seconds=1, batch_size=1)

    async def embed(self, texts: Any) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


class EndpointStubRetriever(ChatRetriever):
    """端点级 stub：零命中（兜底路径离线确定，不触 LLM）。"""

    def __init__(self) -> None:
        super().__init__(embedder=_StubEmbedder(), qdrant_url=None)

    async def search_hits(
        self, *, tenant_id: Any, user_id: Any, query: str
    ) -> list[dict[str, Any]]:
        return []

    async def load_texts(
        self, tenant_id: Any, hits: Sequence[dict[str, Any]]
    ) -> list[ChatEvidence]:
        return []


class _StubAudit:
    def append(self, event: Any) -> Any:
        return event

    async def flush(self) -> int:
        return 0


def make_loop() -> FastPathLoop:
    """零命中 Loop（事件序/编码离线可断言）。"""

    return FastPathLoop(
        llm_port=FakeLlm(),
        retriever=EndpointStubRetriever(),
        prompt_registry=build_default_registry(),
        trace_sink=InMemoryTraceSink(),
        audit_sink=_StubAudit(),
    )


def make_principal(tenant_id: Any) -> Principal:
    """构造端点依赖用的 Principal（绕过 JWT 依赖——RBAC 矩阵真实栈另行覆盖）。"""

    return Principal(
        user_id=uuid4(),
        realm="tenant",
        tenant_id=tenant_id,
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        must_change_password=False,
        roles=frozenset({"student"}),
        permissions=frozenset({"campus.chat.use"}),
    )


def build_app(loop: FastPathLoop, *, tenant_id: Any) -> Any:
    """构造应用 + Principal 依赖覆盖（RBAC 矩阵路径的离线替身）。"""

    app = create_app(chat_loop=loop)
    # 覆盖子依赖 get_current_principal（实证有效）：_dependency 的 realm/权限判定保持真实
    app.dependency_overrides[get_current_principal] = lambda: make_principal(tenant_id)
    return app


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """SSE 文本 → [(event, data_dict)]。"""

    events: list[tuple[str, dict[str, Any]]] = []
    current_event: str | None = None
    for line in text.splitlines():
        if line.startswith("event:"):
            current_event = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current_event is not None:
            events.append((current_event, json.loads(line.split(":", 1)[1])))
            current_event = None
    return events


@pytest.mark.asyncio
async def test_sse_stream_emits_contract_order_and_content_type() -> None:
    """SSE：content-type 正确；事件序=零命中兜底链；meta 回显 turn/conversation。"""

    app = build_app(make_loop(), tenant_id=uuid4())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/campus/chat/turns", json={"message": "校历"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert [name for name, _ in events] == [
        "meta",
        "step",
        "step",
        "tool_call",
        "tool_result",
        "reasoning_summary",
        "delta",
        "done",
    ]
    meta = events[0][1]
    assert meta["turn_id"] and meta["trace_id"] and meta["conversation_id"]
    tool_result = events[4][1]
    assert tool_result["tool"] == "kb.search"
    assert tool_result["status"] == "ok"
    assert tool_result["duration_ms"] >= 0  # 真实耗时（stub 检索也有计时）
    assert tool_result["hits"] == 0
    delta = events[6][1]
    assert delta["text"] == CAPABILITY_FALLBACK_TEXT
    done = events[7][1]
    assert done["stop_reason"] == "CAPABILITY_FALLBACK"


@pytest.mark.asyncio
async def test_conversation_id_is_echoed_when_valid_uuid() -> None:
    """合法客户端 conversation_id 沿用并经 meta 回显（服务端不另分配）。"""

    conversation = str(uuid4())
    app = build_app(make_loop(), tenant_id=uuid4())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/campus/chat/turns",
            json={"message": "校历", "conversation_id": conversation},
        )
    meta = parse_sse(response.text)[0][1]
    assert meta["conversation_id"] == conversation


@pytest.mark.asyncio
async def test_missing_tenant_context_is_403() -> None:
    """租户身份缺失（tenant_id None）：403。"""

    app = build_app(make_loop(), tenant_id=None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/campus/chat/turns", json={"message": "校历"})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_blank_message_is_422() -> None:
    """空消息：422（Pydantic min_length）。"""

    app = build_app(make_loop(), tenant_id=uuid4())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/campus/chat/turns", json={"message": ""})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_chat_loop_missing_is_explicit_error() -> None:
    """chat_loop 未装配：显式失败（≥500），不静默空响应。"""

    app = create_app()
    app.state.chat_loop = None
    app.dependency_overrides[get_current_principal] = lambda: make_principal(uuid4())
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/campus/chat/turns", json={"message": "校历"})
    assert response.status_code >= 500
