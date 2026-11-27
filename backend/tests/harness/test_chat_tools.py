"""chat 只读工具校验（M4-AI01b 步 2）。

三工具 spec（只读/权限/超时）、kb.search 命中元数据形状、kb.read_chunk 证据
白名单守卫（防凭 id 越过人群过滤读任意块）、campus.navigate 静态入口。
"""

from typing import Any
from uuid import uuid4

from app.harness.audit import InMemoryAuditSink
from app.harness.chat_tools import (
    CAMPUS_NAVIGATE,
    KB_READ_CHUNK,
    KB_SEARCH,
    TurnToolState,
    build_turn_tools,
)
from app.harness.contracts import TenantContext, ToolCall, ToolResult, ToolSpec
from app.harness.registry import ToolRegistry
from app.harness.tools import ToolExecutor
from app.harness.trace import InMemoryTraceSink, TraceRecorder
from app.kb.chat_retrieval import ChatEvidence, ChatRetrievalError, ChatRetriever
from app.kb.embedding import TEIEmbedder


class StubEmbedder(TEIEmbedder):
    """无网络 embedder（继承真实类以通过类型面）。"""

    def __init__(self) -> None:
        super().__init__(base_url="http://127.0.0.1:1", timeout_seconds=1, batch_size=1)

    async def embed(self, texts: Any) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


class StubRetriever(ChatRetriever):
    """检索 stub：返回预置命中（无文本——卡 3-A 工具面只含元数据）。"""

    def __init__(self, hits: list[dict[str, Any]] | Exception | None = None) -> None:
        super().__init__(embedder=StubEmbedder(), qdrant_url=None)
        self.hits = hits if hits is not None else []
        self.calls: list[tuple[object, object, object]] = []

    async def search_hits(
        self, *, tenant_id: Any, user_id: Any, query: str
    ) -> list[dict[str, Any]]:
        self.calls.append((tenant_id, user_id, query))
        if isinstance(self.hits, Exception):
            raise self.hits
        return self.hits


def tenant_ctx() -> TenantContext:
    """构造工具上下文（租户/用户均测试值）。"""

    return TenantContext(tenant_id=uuid4(), user_id=uuid4())


def spec_of(registry: ToolRegistry, name: str) -> ToolSpec:
    """按名取 spec。"""

    return registry.get_spec(name)


def test_registry_has_three_readonly_tools() -> None:
    """三工具注册且全部只读；权限挂既有码（D2）。"""

    registry = build_turn_tools(retriever=StubRetriever(), state=TurnToolState())
    assert sorted(spec.name for spec in registry.all()) == [
        CAMPUS_NAVIGATE,
        KB_READ_CHUNK,
        KB_SEARCH,
    ]
    for name in (KB_SEARCH, KB_READ_CHUNK, CAMPUS_NAVIGATE):
        spec = spec_of(registry, name)
        assert spec.side_effect is False
        assert spec.idempotent is True
    assert spec_of(registry, KB_SEARCH).permission == "campus.chat.use"
    assert spec_of(registry, KB_READ_CHUNK).permission == "campus.chat.use"
    assert spec_of(registry, CAMPUS_NAVIGATE).permission == "campus.nav"


async def test_kb_search_returns_hit_metadata_without_text() -> None:
    """kb.search 命中只含元数据：键面无 text（证据文本归编排器回查）。"""

    retriever = StubRetriever(
        hits=[
            {
                "chunk_id": "c1",
                "score": 0.5,
                "payload": {
                    "doc_title": "校历",
                    "section_path": None,
                    "page_start": 1,
                    "page_end": 1,
                    "chunk_type": "child",
                    "version_id": "v1",
                },
            }
        ]
    )
    registry = build_turn_tools(retriever=retriever, state=TurnToolState())
    handler = registry.get_handler(KB_SEARCH)
    result = await handler(ToolCall(tool=KB_SEARCH, args={"query": "校历"}), tenant_ctx())
    assert isinstance(result, ToolResult)
    assert result.ok is True
    hits = result.data["hits"]
    assert len(hits) == 1
    # 原始形状（payload 内嵌）供 loop 证据回查；工具面无证据文本
    assert hits[0]["payload"]["doc_title"] == "校历"
    assert "text" not in hits[0]
    assert "1 条" in result.redacted_summary


async def test_kb_search_handler_maps_retrieval_error_code() -> None:
    """检索基础设施异常 → 结构化失败（错误码透传，异常文本不进结果）。"""

    retriever = StubRetriever(hits=ChatRetrievalError("CHAT_SEARCH_FAILED"))
    registry = build_turn_tools(retriever=retriever, state=TurnToolState())
    handler = registry.get_handler(KB_SEARCH)
    result = await handler(ToolCall(tool=KB_SEARCH, args={"query": "校历"}), tenant_ctx())
    assert result.ok is False
    assert result.error_code == "CHAT_SEARCH_FAILED"


async def test_tenant_id_in_tool_args_is_rejected() -> None:
    """工具面不接受模型/调用方伪造 tenant_id；租户只来自 TenantContext。"""

    registry = build_turn_tools(retriever=StubRetriever(), state=TurnToolState())
    executor = ToolExecutor(
        registry,
        trace=TraceRecorder(InMemoryTraceSink()),
        audit=InMemoryAuditSink("fake-audit-secret-0123456789"),
    )
    ctx = TenantContext(
        tenant_id=uuid4(),
        user_id=uuid4(),
        permissions=frozenset({"campus.chat.use"}),
    )
    result = await executor.execute(
        ToolCall(
            tool=KB_SEARCH,
            args={"query": "本校校历", "tenant_id": str(uuid4())},
        ),
        ctx,
    )

    assert result.ok is False
    assert result.error_code == "INVALID_ARGS"


async def test_kb_read_chunk_allows_only_turn_evidence() -> None:
    """证据映射守卫：本轮证据内的块可读（含文本/版本），映射外拒绝（防越人群读任意块）。"""

    state = TurnToolState()
    state.set_evidence(
        [
            ChatEvidence(
                chunk_id="c1",
                score=0.5,
                text="证据文本",
                doc_title="校历",
                section_path=None,
                page_start=1,
                page_end=1,
                chunk_type="child",
                version_id="v1",
            )
        ]
    )
    registry = build_turn_tools(retriever=StubRetriever(), state=state)
    handler = registry.get_handler(KB_READ_CHUNK)

    inside = await handler(ToolCall(tool=KB_READ_CHUNK, args={"chunk_id": "c1"}), tenant_ctx())
    assert inside.ok is True
    assert inside.data == {"chunk_id": "c1", "text": "证据文本", "version_id": "v1"}

    outside = await handler(
        ToolCall(tool=KB_READ_CHUNK, args={"chunk_id": str(uuid4())}), tenant_ctx()
    )
    assert outside.ok is False
    assert outside.error_code == "CHUNK_NOT_IN_EVIDENCE"

    malformed = await handler(
        ToolCall(tool=KB_READ_CHUNK, args={"chunk_id": "not-a-uuid"}), tenant_ctx()
    )
    assert malformed.ok is False
    assert malformed.error_code == "CHUNK_NOT_IN_EVIDENCE"


async def test_navigate_returns_static_entry() -> None:
    """campus.navigate：静态入口映射，未知目标结构化拒绝。"""

    registry = build_turn_tools(retriever=StubRetriever(), state=TurnToolState())
    handler = registry.get_handler(CAMPUS_NAVIGATE)
    known = await handler(ToolCall(tool=CAMPUS_NAVIGATE, args={"target": "请假"}), tenant_ctx())
    assert known.ok is True
    assert known.data["route"]
    unknown = await handler(
        ToolCall(tool=CAMPUS_NAVIGATE, args={"target": "不存在入口"}), tenant_ctx()
    )
    assert unknown.ok is False
    assert unknown.error_code == "NAVIGATE_TARGET_UNKNOWN"


def test_turn_tool_state_defaults_empty() -> None:
    """回合状态默认无证据映射（RETRIEVE 前读块一律拒绝）。"""

    state = TurnToolState()
    assert state.evidence_by_id == {}
