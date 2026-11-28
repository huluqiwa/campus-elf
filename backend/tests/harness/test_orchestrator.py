"""ChatOrchestrator 分派校验（M4-AI01c 步 4；决策卡 1-A/D1/D2 + TD-334）。"""

import json
from typing import Any
from uuid import uuid4

from app.harness.llm import LlmError, LlmErrorCode, LlmResponse, LlmStreamEvent
from app.harness.orchestrator import ChatOrchestrator
from app.harness.prompt_loading import build_default_registry
from app.harness.router import LlmRouter
from app.harness.trace import InMemoryTraceSink
from app.kb.chat_retrieval import ChatEvidence, ChatRetriever
from app.kb.embedding import TEIEmbedder
from tests.support.llm import FakeLlm


class _StubEmbedder(TEIEmbedder):
    """无网络 embedder（继承真实类以通过类型面）。"""

    def __init__(self) -> None:
        super().__init__(base_url="http://127.0.0.1:1", timeout_seconds=1, batch_size=1)

    async def embed(self, texts: Any) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


def stub_retriever(evidence: list[ChatEvidence] | None = None) -> Any:
    """检索 stub（与 react/loop 测试同构）。"""

    class _Stub(ChatRetriever):
        def __init__(self) -> None:
            super().__init__(embedder=_StubEmbedder(), qdrant_url=None)
            self._evidence = evidence

        async def search_hits(
            self, *, tenant_id: Any, user_id: Any, query: str
        ) -> list[dict[str, Any]]:
            if self._evidence is None:
                return []
            return [
                {
                    "chunk_id": item.chunk_id,
                    "score": 1.0,
                    "payload": {"doc_title": item.doc_title, "version_id": item.version_id},
                }
                for item in self._evidence
            ]

        async def load_texts(self, tenant_id: Any, hits: Any) -> list[ChatEvidence]:
            return list(self._evidence or [])

    return _Stub()


def build_orchestrator(
    *,
    router_complete: Any,
    fast_llm: Any,
    react_llm: Any,
    evidence: list[ChatEvidence] | None = None,
) -> tuple[ChatOrchestrator, InMemoryTraceSink]:
    """装配被测 orchestrator（router/fast/react 各自独立 FakeLlm）；返回 sink 供断言。"""

    from app.harness.trace import InMemoryTraceSink

    registry = build_default_registry()
    router = LlmRouter(
        llm_port=FakeLlm(complete_scripts=[router_complete]),
        prompt_registry=registry,
    )
    fast_loop = FastPathLoopFactory(evidence=evidence, llm=fast_llm, registry=registry)
    sink = InMemoryTraceSink()
    react_loop = ReactLoopFactory(evidence=evidence, llm=react_llm, registry=registry, sink=sink)
    return ChatOrchestrator(router=router, fast_loop=fast_loop, react_loop=react_loop), sink


def FastPathLoopFactory(*, evidence: Any, llm: Any, registry: Any) -> Any:
    from app.harness.loop import FastPathLoop
    from app.harness.trace import InMemoryTraceSink

    class _Audit:
        def append(self, event: Any) -> Any:
            return event

        async def flush(self) -> int:
            return 0

    return FastPathLoop(
        llm_port=llm,
        retriever=stub_retriever(evidence),
        prompt_registry=registry,
        trace_sink=InMemoryTraceSink(),
        audit_sink=_Audit(),
    )


def ReactLoopFactory(*, evidence: Any, llm: Any, registry: Any, sink: Any) -> Any:
    from app.harness.react_loop import ReactLoop

    class _Audit:
        def append(self, event: Any) -> Any:
            return event

        async def flush(self) -> int:
            return 0

    return ReactLoop(
        llm_port=llm,
        retriever=stub_retriever(evidence),
        prompt_registry=registry,
        trace_sink=sink,
        audit_sink=_Audit(),
    )


async def collect(aiter: Any) -> list[Any]:
    return [event async for event in aiter]


def types_of(events: list[Any]) -> list[str]:
    return [event.type.value for event in events]


def router_json_single() -> LlmResponse:
    """单 kb_qa 路由 JSON（simple）。"""

    return LlmResponse(
        text=json.dumps(
            {
                "scope": "allow",
                "blocked_count": 0,
                "safe_fragments": [],
                "intents": [{"name": "kb_qa", "query": "校历", "confidence": 0.9}],
                "safety": {"action": "allow", "category": "none"},
            }
        )
    )


def router_json_multi() -> LlmResponse:
    """双意图路由 JSON。"""

    return LlmResponse(
        text=json.dumps(
            {
                "scope": "allow",
                "blocked_count": 0,
                "safe_fragments": [],
                "intents": [
                    {"name": "kb_qa", "query": "差旅报销怎么办理？", "confidence": 0.9},
                    {"name": "navigate", "query": "请假入口在哪？", "confidence": 0.9},
                ],
                "safety": {"action": "allow", "category": "none"},
            }
        )
    )


async def test_simple_intent_dispatches_fast_path_order_unchanged() -> None:
    """simple 路由 → 快路径管道；事件序与 AI-01b 逐字节一致（回归面）。"""

    evidence = [
        ChatEvidence(
            chunk_id="c1",
            score=0.9,
            text="内容。",
            doc_title="校历",
            section_path=None,
            page_start=None,
            page_end=None,
            chunk_type="child",
            version_id="v1",
        )
    ]
    orchestrator, _sink = build_orchestrator(
        router_complete=router_json_single(),
        fast_llm=FakeLlm(
            stream_scripts=[
                [
                    LlmStreamEvent(kind="delta", text_delta="答 [1]。"),
                    LlmStreamEvent(kind="finish", finish_reason="stop"),
                ]
            ],
        ),
        react_llm=FakeLlm(),
        evidence=evidence,
    )
    events = await collect(
        orchestrator.run(
            tenant_id=uuid4(),
            user_id=uuid4(),
            turn_id=uuid4(),
            message="校历是什么？",
            permissions=frozenset({"campus.chat.use"}),
            persist=False,
        )
    )

    # AI-04：citation=被引子集——脚本答案引用 [1] 才有 citation 事件
    assert types_of(events) == [
        "meta",
        "step",
        "step",
        "tool_call",
        "tool_result",
        "reasoning_summary",
        "delta",
        "citation",
        "done",
    ]
    # meta 只出现一次（loop 跳过自身 route 段——D1）
    assert types_of(events).count("meta") == 1


async def test_multi_intent_dispatches_react() -> None:
    """多意图路由 → ReAct 路径（route_path=react）。"""

    evidence = [
        ChatEvidence(
            chunk_id="c1",
            score=0.9,
            text="内容。",
            doc_title="校历",
            section_path=None,
            page_start=None,
            page_end=None,
            chunk_type="child",
            version_id="v1",
        )
    ]
    orchestrator, _sink = build_orchestrator(
        router_complete=router_json_multi(),
        fast_llm=FakeLlm(),
        react_llm=FakeLlm(
            complete_scripts=[
                LlmResponse(text="DONE", finish_reason="stop"),
            ],
            stream_scripts=[
                [
                    LlmStreamEvent(kind="delta", text_delta="分段回答 [1]。"),
                    LlmStreamEvent(kind="finish", finish_reason="stop"),
                ]
            ],
        ),
        evidence=evidence,
    )
    events = await collect(
        orchestrator.run(
            tenant_id=uuid4(),
            user_id=uuid4(),
            turn_id=uuid4(),
            message="差旅报销怎么办理？另外请假入口在哪？",
            permissions=frozenset({"campus.chat.use", "campus.nav"}),
            persist=False,
        )
    )

    assert types_of(events)[0] == "meta"
    assert types_of(events)[-1] == "done"
    assert types_of(events).count("meta") == 1
    summaries = [
        event.payload.get("summary", "")
        for event in events
        if event.type.value == "reasoning_summary"
    ]
    assert any("识别到 2 个问题" in summary for summary in summaries)


async def test_router_failure_stops_before_data_plane() -> None:
    """范围路由器异常时保留固定脱敏错误，不把原文送入检索或答案模型。"""

    fast_llm = FakeLlm(
        stream_scripts=[
            [
                LlmStreamEvent(kind="delta", text_delta="不应生成。"),
                LlmStreamEvent(kind="finish", finish_reason="stop"),
            ]
        ],
    )
    orchestrator, _sink = build_orchestrator(
        router_complete=LlmError(LlmErrorCode.TIMEOUT, retryable=True),
        fast_llm=fast_llm,
        react_llm=FakeLlm(),
        evidence=None,
    )
    events = await collect(
        orchestrator.run(
            tenant_id=uuid4(),
            user_id=uuid4(),
            turn_id=uuid4(),
            message="校历是什么？",
            permissions=frozenset({"campus.chat.use"}),
            persist=False,
        )
    )

    assert types_of(events) == ["meta", "step", "delta", "done"]
    assert events[-1].payload["stop_reason"] == "GUARD_BLOCK"
    assert "无法安全处理" in events[2].payload["text"]
    assert fast_llm.requests == []


async def test_permissions_forwarded_to_loops() -> None:
    """TD-334：principal 权限集透传 loop（缺 nav 权限 → navigate 拒绝）。"""

    evidence = [
        ChatEvidence(
            chunk_id="c1",
            score=0.9,
            text="内容。",
            doc_title="校历",
            section_path=None,
            page_start=None,
            page_end=None,
            chunk_type="child",
            version_id="v1",
        )
    ]
    orchestrator, sink = build_orchestrator(
        router_complete=router_json_multi(),
        fast_llm=FakeLlm(),
        react_llm=FakeLlm(
            complete_scripts=[
                LlmResponse(
                    text="",
                    finish_reason="tool_calls",
                    tool_calls=(
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "campus.navigate",
                                "arguments": json.dumps({"target": "请假"}, ensure_ascii=False),
                            },
                        },
                    ),
                ),
                LlmResponse(text="DONE", finish_reason="stop"),
            ],
            stream_scripts=[
                [
                    LlmStreamEvent(kind="delta", text_delta="入口见 [1]。"),
                    LlmStreamEvent(kind="finish", finish_reason="stop"),
                ]
            ],
        ),
        evidence=evidence,
    )
    events = await collect(
        orchestrator.run(
            tenant_id=uuid4(),
            user_id=uuid4(),
            turn_id=uuid4(),
            message="差旅报销怎么办理？另外请假入口在哪？",
            permissions=frozenset({"campus.chat.use"}),
            persist=False,  # 无 campus.nav
        )
    )

    tool_results = [event.payload for event in events if event.type.value == "tool_result"]
    denied = [
        payload
        for payload in tool_results
        if payload.get("status") == "error" and payload.get("tool") == "campus.navigate"
    ]
    assert denied  # navigate 被权限拒绝（TD-334 端到端）
    # 审查 m4：错误码精确锚定（executor 写入 trace span）
    denied_spans = [
        span
        for span in sink.spans
        if span.span_type == "tool" and span.attributes.get("error_code") == "PERMISSION_DENIED"
    ]
    assert denied_spans
    assert types_of(events)[-1] == "done"
