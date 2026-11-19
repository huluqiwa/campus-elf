"""ReactLoop 循环校验（M4-AI01c 步 3；决策卡 3-A/4-A/5-A、D2-D5）。"""

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import uuid4

from app.harness.contracts import Intent, RouteResult
from app.harness.llm import LlmError, LlmErrorCode, LlmResponse, LlmStreamEvent
from app.harness.prompt_loading import build_default_registry
from app.harness.react_loop import ReactLoop
from app.harness.router import RouterOutcome
from app.harness.trace import InMemoryTraceSink
from app.kb.chat_retrieval import ChatEvidence, ChatRetriever
from app.kb.embedding import TEIEmbedder
from tests.support.llm import FakeLlm


class StubEmbedder(TEIEmbedder):
    """无网络 embedder（继承真实类以通过类型面）。"""

    def __init__(self) -> None:
        super().__init__(base_url="http://127.0.0.1:1", timeout_seconds=1, batch_size=1)

    async def embed(self, texts: Any) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


class StubRetriever(ChatRetriever):
    """检索 stub：search_hits/load_texts 返回预置证据。"""

    def __init__(self, evidence: list[ChatEvidence] | None = None) -> None:
        super().__init__(embedder=StubEmbedder(), qdrant_url=None)
        self._evidence = evidence
        self.load_calls = 0

    async def search_hits(
        self, *, tenant_id: Any, user_id: Any, query: str
    ) -> list[dict[str, Any]]:
        if self._evidence is None:
            return []
        return [
            {
                "chunk_id": item.chunk_id,
                "score": item.score,
                "payload": {
                    "doc_title": item.doc_title,
                    "section_path": item.section_path,
                    "page_start": item.page_start,
                    "page_end": item.page_end,
                    "chunk_type": item.chunk_type,
                    "version_id": item.version_id,
                },
            }
            for item in self._evidence
        ]

    async def load_texts(
        self, tenant_id: Any, hits: Sequence[dict[str, Any]]
    ) -> list[ChatEvidence]:
        self.load_calls += 1
        return list(self._evidence or [])


def tool_call(name: str, arguments: str) -> dict[str, Any]:
    """SDK 已拼形态的 tool_call dict（complete 路径）。"""

    return {
        "id": f"call-{name}",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def kb_response(calls: list[dict[str, Any]] | None = None, text: str = "") -> LlmResponse:
    """THINK 决策响应（tool_calls 或纯文本 DONE）。"""

    return LlmResponse(text=text, finish_reason="tool_calls" if calls else "stop",
                       tool_calls=tuple(calls or ()))


def two_intent_route() -> RouterOutcome:
    """双意图路由结果（kb_qa + navigate）。"""

    return RouterOutcome(
        route=RouteResult(
            intents=[
                Intent(name="kb_qa", confidence=0.9, args={"query": "差旅报销怎么办理？"}),
                Intent(name="navigate", confidence=0.8, args={"query": "请假入口在哪？"}),
            ],
            primary_capability="kb_qa",
        ),
        simple=False,
    )


def evidence(chunk_id: str, text: str, title: str = "校历") -> ChatEvidence:
    """构造一条证据。"""

    return ChatEvidence(
        chunk_id=chunk_id,
        score=0.9,
        text=text,
        doc_title=title,
        section_path="第一章",
        page_start=3,
        page_end=3,
        chunk_type="child",
        version_id="v1",
    )


def build_loop(
    llm_port: Any,
    retriever: StubRetriever,
    *,
    permissions: frozenset[str] | None = None,
    **overrides: Any,
) -> tuple[ReactLoop, InMemoryTraceSink, Any]:
    """构造被测 ReactLoop（内存 trace sink 供断言）。"""

    sink = InMemoryTraceSink()
    audit = _StubAudit()
    loop = ReactLoop(
        llm_port=llm_port,
        retriever=retriever,
        prompt_registry=build_default_registry(),
        trace_sink=sink,
        audit_sink=audit,
        **overrides,
    )
    return loop, sink, audit


class _StubAudit:
    def __init__(self) -> None:
        self.flushed = 0

    def append(self, event: Any) -> Any:
        return event

    async def flush(self) -> int:
        self.flushed += 1
        return 0


async def collect(aiter: AsyncIterator[Any]) -> list[Any]:
    """收集事件流。"""

    return [event async for event in aiter]


def types_of(events: list[Any]) -> list[str]:
    return [event.type.value for event in events]


async def test_react_happy_path_search_then_navigate() -> None:
    """全链：THINK search → OBSERVE → THINK navigate → GENERATE → citations（全局编号）。"""

    retriever = StubRetriever(evidence=[evidence("c1", "报销需发票。", title="财务手册")])
    fake = FakeLlm(
        complete_scripts=[
            kb_response([tool_call("kb.search", json.dumps({"query": "差旅报销"}, ensure_ascii=False))]),
            kb_response([tool_call("campus.navigate", json.dumps({"target": "请假"}, ensure_ascii=False))]),
            kb_response(text="DONE"),
        ],
        stream_scripts=[
            [
                LlmStreamEvent(kind="delta", text_delta="报销需发票 [1]；请假入口 [2]。"),
                LlmStreamEvent(kind="finish", finish_reason="stop"),
                LlmStreamEvent(kind="usage", tokens_in=200, tokens_out=20),
            ]
        ],
    )
    loop, sink, audit = build_loop(fake, retriever)
    events = await collect(
        loop.run(
            tenant_id=uuid4(),
            user_id=uuid4(),
            turn_id=uuid4(),
            message="差旅报销怎么办理？另外请假入口在哪？",
            route=two_intent_route(),
        )
    )

    names = types_of(events)
    assert names[0] == "meta"
    assert "step" in names and "tool_call" in names
    assert names[-1] == "done"
    # THINK 决策 3 次（两次工具 + 一次 DONE）
    tool_results = [event.payload for event in events if event.type.value == "tool_result"]
    assert tool_results[0]["status"] == "ok"
    assert tool_results[1]["status"] == "ok"
    citations = [event.payload for event in events if event.type.value == "citation"]
    assert [item["index"] for item in citations] == [1, 2]
    assert citations[0]["document"] == "财务手册"
    assert citations[1]["document"] == "业务入口"  # navigate 结果作为入口证据
    done = events[-1].payload
    assert done["stop_reason"] == "FINAL_ANSWER"
    # GENERATE prompt 含逐意图小节要求与证据
    generate_prompt = fake.requests[-1].messages[0].content
    assert "差旅报销怎么办理？" in generate_prompt
    assert "[1]" in generate_prompt
    assert audit.flushed == 1
    # trace：route_path=react + budget span
    assert sink.turns[-1].route_path == "react"
    assert sink.turns[-1].intents == ["kb_qa", "navigate"]
    budget_spans = [span for span in sink.spans if span.span_type == "budget"]
    assert budget_spans and budget_spans[-1].attributes["tool_calls"] == 2


async def test_budget_tool_calls_exhausted_degrades_to_generate() -> None:
    """工具预算耗尽（决策卡 5-A）：剩余调用不执行，带已有证据 GENERATE。"""

    retriever = StubRetriever(evidence=[evidence("c1", "内容。")])
    fake = FakeLlm(
        complete_scripts=[
            kb_response([tool_call("kb.search", json.dumps({"query": "q1"}, ensure_ascii=False))]),
            kb_response([tool_call("kb.search", json.dumps({"query": "q2"}, ensure_ascii=False))]),
            kb_response([tool_call("kb.search", json.dumps({"query": "q3"}, ensure_ascii=False))]),
        ],
        stream_scripts=[
            [LlmStreamEvent(kind="delta", text_delta="回答 [1]。"),
             LlmStreamEvent(kind="finish", finish_reason="stop")],
        ],
    )
    loop, sink, _audit = build_loop(fake, retriever, max_tool_calls=2)
    events = await collect(
        loop.run(
            tenant_id=uuid4(),
            user_id=uuid4(),
            turn_id=uuid4(),
            message="问题",
            route=RouterOutcome(
                route=RouteResult(
                    intents=[Intent(name="kb_qa", confidence=0.9, args={"query": "问题"})],
                    primary_capability="kb_qa",
                ),
                simple=False,
            ),
        )
    )

    tool_results = [event.payload for event in events if event.type.value == "tool_result"]
    assert len(tool_results) == 2  # 第 3 次调用被预算拦截
    assert types_of(events)[-1] == "done"
    assert events[-1].payload["stop_reason"] == "FINAL_ANSWER"
    budget_spans = [span for span in sink.spans if span.span_type == "budget"]
    assert budget_spans[-1].attributes["tool_calls"] == 2


async def test_budget_decisions_exhausted_degrades() -> None:
    """决策预算耗尽：不再发第 3 次决策，直接 GENERATE。"""

    retriever = StubRetriever(evidence=[evidence("c1", "内容。")])
    fake = FakeLlm(
        complete_scripts=[
            kb_response([tool_call("kb.search", json.dumps({"query": "q1"}, ensure_ascii=False))]),
            kb_response([tool_call("kb.search", json.dumps({"query": "q2"}, ensure_ascii=False))]),
            kb_response([tool_call("kb.search", json.dumps({"query": "q3"}, ensure_ascii=False))]),
        ],
        stream_scripts=[
            [LlmStreamEvent(kind="delta", text_delta="回答。"),
             LlmStreamEvent(kind="finish", finish_reason="stop")],
        ],
    )
    loop, _sink, _audit = build_loop(fake, retriever, max_decisions=2)
    events = await collect(
        loop.run(
            tenant_id=uuid4(),
            user_id=uuid4(),
            turn_id=uuid4(),
            message="问题",
            route=RouterOutcome(
                route=RouteResult(
                    intents=[Intent(name="kb_qa", confidence=0.9, args={"query": "问题"})],
                    primary_capability="kb_qa",
                ),
                simple=False,
            ),
        )
    )

    think_steps = [event.payload for event in events if event.type.value == "step" and event.payload.get("stage") == "think"]
    assert len(think_steps) == 2  # 第 3 次决策被预算拦截
    assert types_of(events)[-1] == "done"


async def test_read_chunk_outside_evidence_feedback_continues() -> None:
    """白名单反馈弧：未检索先读块 → CHUNK_NOT_IN_EVIDENCE 观察回喂 → 循环继续至完成。"""

    retriever = StubRetriever(evidence=[evidence("c1", "内容。")])
    fake = FakeLlm(
        complete_scripts=[
            kb_response([tool_call("kb.read_chunk", json.dumps({"chunk_id": "ghost"}, ensure_ascii=False))]),
            kb_response([tool_call("kb.search", json.dumps({"query": "q"}, ensure_ascii=False))]),
            kb_response(text="DONE"),
        ],
        stream_scripts=[
            [LlmStreamEvent(kind="delta", text_delta="回答 [1]。"),
             LlmStreamEvent(kind="finish", finish_reason="stop")],
        ],
    )
    loop, _sink, _audit = build_loop(fake, retriever)
    events = await collect(
        loop.run(
            tenant_id=uuid4(),
            user_id=uuid4(),
            turn_id=uuid4(),
            message="问题",
            route=RouterOutcome(
                route=RouteResult(
                    intents=[Intent(name="kb_qa", confidence=0.9, args={"query": "问题"})],
                    primary_capability="kb_qa",
                ),
                simple=False,
            ),
        )
    )

    tool_results = [event.payload for event in events if event.type.value == "tool_result"]
    assert tool_results[0]["status"] == "error"  # 白名单外拒绝（反馈不炸循环）
    assert tool_results[1]["status"] == "ok"
    assert types_of(events)[-1] == "done"


async def test_navigate_unknown_target_falls_back() -> None:
    """navigate 未知入口：观察反馈 → 证据空 → 兜底 CAPABILITY_FALLBACK。"""

    retriever = StubRetriever(evidence=None)
    fake = FakeLlm(
        complete_scripts=[
            kb_response([tool_call("campus.navigate", json.dumps({"target": "不存在入口"}, ensure_ascii=False))]),
            kb_response(text="DONE"),
        ],
    )
    loop, _sink, _audit = build_loop(fake, retriever)
    events = await collect(
        loop.run(
            tenant_id=uuid4(),
            user_id=uuid4(),
            turn_id=uuid4(),
            message="不存在入口在哪？",
            route=RouterOutcome(
                route=RouteResult(
                    intents=[Intent(name="navigate", confidence=0.9, args={"query": "不存在入口在哪？"})],
                    primary_capability="navigate",
                ),
                simple=False,
            ),
        )
    )

    assert types_of(events)[-1] == "done"
    assert events[-1].payload["stop_reason"] == "CAPABILITY_FALLBACK"


async def test_missing_nav_permission_denies_navigate_tool() -> None:
    """TD-334：permissions 从 principal 派生——缺 campus.nav 时 navigate 调用 PERMISSION_DENIED。"""

    retriever = StubRetriever(evidence=None)
    fake = FakeLlm(
        complete_scripts=[
            kb_response([tool_call("campus.navigate", json.dumps({"target": "请假"}, ensure_ascii=False))]),
            kb_response(text="DONE"),
        ],
    )
    loop, sink, _audit = build_loop(fake, retriever)
    events = await collect(
        loop.run(
            tenant_id=uuid4(),
            user_id=uuid4(),
            turn_id=uuid4(),
            message="请假入口在哪？",
            route=RouterOutcome(
                route=RouteResult(
                    intents=[Intent(name="navigate", confidence=0.9, args={"query": "请假入口在哪？"})],
                    primary_capability="navigate",
                ),
                simple=False,
            ),
            permissions=frozenset({"campus.chat.use"}),  # 无 campus.nav
        )
    )

    tool_results = [event.payload for event in events if event.type.value == "tool_result"]
    assert tool_results[0]["status"] == "error"
    assert types_of(events)[-1] == "done"
    assert sink.turns[-1].stop_reason is not None
    assert sink.turns[-1].stop_reason.value == "CAPABILITY_FALLBACK"


async def test_zero_evidence_overall_falls_back() -> None:
    """全意图零证据：检索后直接兜底（不进 GENERATE）。"""

    retriever = StubRetriever(evidence=None)
    fake = FakeLlm(
        complete_scripts=[
            kb_response([tool_call("kb.search", json.dumps({"query": "q"}, ensure_ascii=False))]),
            kb_response(text="DONE"),
        ],
    )
    loop, _sink, _audit = build_loop(fake, retriever)
    events = await collect(
        loop.run(
            tenant_id=uuid4(),
            user_id=uuid4(),
            turn_id=uuid4(),
            message="问题",
            route=RouterOutcome(
                route=RouteResult(
                    intents=[Intent(name="kb_qa", confidence=0.9, args={"query": "问题"})],
                    primary_capability="kb_qa",
                ),
                simple=False,
            ),
        )
    )

    assert types_of(events)[-1] == "done"
    assert events[-1].payload["stop_reason"] == "CAPABILITY_FALLBACK"


async def test_standalone_mode_emits_route_stage() -> None:
    """独立模式（skip_route_stage=False）：loop 自发 meta+step(route)（兼容直接调用）。"""

    retriever = StubRetriever(evidence=[evidence("c1", "内容。")])
    fake = FakeLlm(
        complete_scripts=[kb_response(text="DONE")],
        stream_scripts=[[LlmStreamEvent(kind="delta", text_delta="答。"),
                         LlmStreamEvent(kind="finish", finish_reason="stop")]],
    )
    loop, _sink, _audit = build_loop(fake, retriever)
    events = await collect(
        loop.run(
            tenant_id=uuid4(),
            user_id=uuid4(),
            turn_id=uuid4(),
            message="问题",
            route=RouterOutcome(
                route=RouteResult(
                    intents=[Intent(name="kb_qa", confidence=0.9, args={"query": "问题"})],
                    primary_capability="kb_qa",
                ),
                simple=False,
            ),
        )
    )

    assert types_of(events)[0] == "meta"
    assert types_of(events)[1] == "step"


async def test_think_llm_error_maps_to_error_event() -> None:
    """THINK 决策 LLM 异常：error(LLM_*) 无 done（决策步非流式，partial=False）。"""

    retriever = StubRetriever(evidence=None)
    fake = FakeLlm(complete_scripts=[LlmError(LlmErrorCode.TIMEOUT, retryable=True)])
    loop, _sink, _audit = build_loop(fake, retriever)
    events = await collect(
        loop.run(
            tenant_id=uuid4(),
            user_id=uuid4(),
            turn_id=uuid4(),
            message="问题",
            route=RouterOutcome(
                route=RouteResult(
                    intents=[Intent(name="kb_qa", confidence=0.9, args={"query": "问题"})],
                    primary_capability="kb_qa",
                ),
                simple=False,
            ),
        )
    )

    assert types_of(events)[-1] == "error"
    assert events[-1].payload["code"] == "LLM_TIMEOUT"
    assert all(name != "done" for name in types_of(events))
