"""qa_runner 集成测试（fake SSE/judge/faithfulness，CI 可跑）：
收集并发 / collected 持久化往返 / 评分编排 / 报告落盘。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from campus_elf_evals.qa_cases import QaCase, load_cases
from campus_elf_evals.qa_collect import TurnResult
from campus_elf_evals.qa_report import aggregate, build_run_meta, write_reports
from campus_elf_evals.qa_runner import (
    _smoke_subset,
    collect_all,
    load_collected,
    load_collected_snapshot,
    save_collected,
    score_all,
)
from campus_elf_evals.qa_score import OpenAIJudge


def _case(cid: str, **overrides) -> QaCase:
    base = dict(
        id=cid,
        category="kb_qa",
        input=f"问题 {cid}",
        expected_points=["要点"],
        expected_citations=["甲办法#条件"],
        forbidden=[],
        rubric="答对要点",
        adversarial=False,
        notes="",
    )
    base.update(overrides)
    return QaCase(**base)


class _ChunkedStream(httpx.AsyncByteStream):
    """逐帧产出字节（真分块，跨 chunk 边界解析）。"""

    def __init__(self, frames: list[str]) -> None:
        self._chunks = [f.encode("utf-8") for f in frames]

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


def _sse_frames(answer: str) -> list[str]:
    return [
        'event: meta\ndata: {"turn_id": "t", "trace_id": "tr", "conversation_id": "c"}\n\n',
        f'event: delta\ndata: {json.dumps({"text": answer[:1]}, ensure_ascii=False)}\n\n',
        ": keep-alive\n\n",
        f'event: delta\ndata: {json.dumps({"text": answer[1:]}, ensure_ascii=False)}\n\n',
        'event: citation\ndata: {"index": 1, "chunk_id": "ch1", "document": "甲办法", '
        '"page": null, "section": "甲办法 > 条件"}\n\n',
        'event: done\ndata: {"stop_reason": "completed"}\n\n',
    ]


def _chat_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v1/campus/chat/turns":
        message = json.loads(request.content)["message"]
        return httpx.Response(
            200,
            stream=_ChunkedStream(_sse_frames(f"要点答案（{message}）")),
            headers={"Content-Type": "text/event-stream"},
        )
    return httpx.Response(404)


@pytest.mark.asyncio
async def test_collect_all_maps_case_ids_to_turns() -> None:
    cases = [_case("kb_qa-001"), _case("kb_qa-002")]
    async with httpx.AsyncClient(transport=httpx.MockTransport(_chat_handler)) as client:
        turns = await collect_all(client, "http://x", "tok", cases, concurrency=2)
    assert set(turns) == {"kb_qa-001", "kb_qa-002"}
    turn = turns["kb_qa-001"]
    assert turn.answer == "要点答案（问题 kb_qa-001）"
    assert turn.done == {"stop_reason": "completed"}
    assert turn.citations[0]["chunk_id"] == "ch1"
    assert turn.first_delta_ms is not None


@pytest.mark.asyncio
async def test_collect_all_refreshes_token_when_aged() -> None:
    cases = [_case("kb_qa-001"), _case("kb_qa-002")]
    used: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        used.append(request.headers.get("Authorization", ""))
        return _chat_handler(request)

    refresh_calls = {"n": 0}

    async def refresh() -> str:
        refresh_calls["n"] += 1
        return f"tok-{refresh_calls['n']}"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        turns = await collect_all(
            client, "http://x", "tok-initial", cases,
            concurrency=1, refresh_token=refresh, refresh_interval_s=0,
        )
    assert set(turns) == {"kb_qa-001", "kb_qa-002"}
    # refresh_interval_s=0 → 每回合前都重登；初始令牌不被使用
    assert used == ["Bearer tok-1", "Bearer tok-2"]
    assert refresh_calls["n"] == 2


def test_collected_roundtrip(tmp_path: Path) -> None:
    turns = {
        "kb_qa-001": TurnResult(
            answer="甲乙", citations=[{"chunk_id": "ch1"}], done={"stop_reason": "completed"},
            meta={"turn_id": "t"}, first_delta_ms=10, latency_ms=99,
        )
    }
    path = tmp_path / "sub" / "collected.json"
    save_collected(turns, path)
    assert load_collected(path) == turns


def test_td369_source_hash_uses_loaded_snapshot_not_later_file(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    cases = load_cases(root / "cases")
    source = tmp_path / "collected.json"
    save_collected(_complete_turns(cases), source)
    snapshot_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    loaded, captured_hash = load_collected_snapshot(source)
    assert set(loaded) == {case.id for case in cases}
    assert captured_hash == snapshot_hash

    source.write_text('{"kb_qa-001":{}}', encoding="utf-8")
    meta = build_run_meta(
        model="synthetic",
        seed=42,
        base_url="http://test.invalid",
        mode="full",
        started_at="2026-11-26T00:00:00Z",
        duration_s=1,
        cases=cases,
        cases_dir=root / "cases",
        collected_path=source,
        source_collection_sha256=captured_hash,
    )
    assert meta["evaluation_contract"]["source_collection_sha256"] == snapshot_hash


class _FakeCompletions:
    async def create(self, **kwargs):
        class _NS:
            pass

        msg = _NS()
        msg.content = '{"score": 2, "redline_hit": false, "reason": "ok"}'
        choice = _NS()
        choice.message = msg
        resp = _NS()
        resp.choices = [choice]
        return resp


class _FakeJudgeClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeCompletions()


@pytest.mark.asyncio
async def test_score_all_faithfulness_happy_error_and_skipped() -> None:
    happy = _case("kb_qa-001")
    boom = _case("kb_qa-002")
    no_cite = _case("kb_qa-003", expected_citations=[])
    turns = {
        happy.id: TurnResult(
            answer="要点",
            citations=[{"chunk_id": "ch1", "document": "甲办法", "section": "甲办法 > 条件"}],
            done={"stop_reason": "completed"},
        ),
        boom.id: TurnResult(
            answer="要点",
            citations=[{"chunk_id": "ch2", "document": "甲办法", "section": "甲办法 > 条件"}],
            done={"stop_reason": "completed"},
        ),
        no_cite.id: TurnResult(answer="要点", done={"stop_reason": "completed"}),
    }

    async def faith(case: QaCase, turn: TurnResult) -> float:
        if case.id == boom.id:
            raise RuntimeError("ragas 故障")
        return 0.95

    judge = OpenAIJudge(client=_FakeJudgeClient(), model="m", seed=42)
    outcomes = await score_all([happy, boom, no_cite], turns, judge, faith)
    by_id = {o.case.id: o for o in outcomes}
    assert by_id[happy.id].faithfulness == 0.95
    assert by_id[happy.id].passed
    # 单条评估故障：如实记录异常类型（脱敏后不保留原始消息），不拖垮整批、不偷偷改判 passed
    assert by_id[boom.id].faithfulness is None
    assert "RuntimeError" in (by_id[boom.id].faithfulness_error or "")
    assert by_id[boom.id].passed
    # 零引用：不评 faithfulness
    assert by_id[no_cite.id].faithfulness is None
    assert by_id[no_cite.id].faithfulness_error is None


def test_smoke_subset_two_per_category() -> None:
    cases = [
        _case(f"kb_qa-{i:03d}") for i in range(1, 4)
    ] + [
        _case(f"refusal-{i:03d}", category="refusal") for i in range(1, 4)
    ]
    subset = _smoke_subset(cases)
    assert [c.id for c in subset] == ["kb_qa-001", "kb_qa-002", "refusal-001", "refusal-002"]


@pytest.mark.asyncio
async def test_end_to_end_outcomes_to_reports(tmp_path: Path) -> None:
    cases = [_case("kb_qa-001"), _case("kb_qa-002")]
    async with httpx.AsyncClient(transport=httpx.MockTransport(_chat_handler)) as client:
        turns = await collect_all(client, "http://x", "tok", cases)

    async def faith(case: QaCase, turn: TurnResult) -> float:
        return 0.95

    judge = OpenAIJudge(client=_FakeJudgeClient(), model="m", seed=42)
    outcomes = await score_all(cases, turns, judge, faith)
    lines = aggregate(outcomes)
    meta = build_run_meta(
        model="m", seed=42, base_url="http://x", mode="smoke",
        started_at="2026-11-24T00:00:00+00:00", duration_s=1.2,
    )
    json_path, md_path = write_reports(outcomes, lines, meta, tmp_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["regression_ids"] == []
    assert len(payload["cases"]) == 2
    assert payload["cases"][0]["faithfulness"] == 0.95
    md = md_path.read_text(encoding="utf-8")
    assert "| overall |" in md and "| faithfulness |" in md
    # 只有 kb_qa 样例：safety/refusal/classification 无线可判 → 不达标（如实）
    by_name = {line.name: line for line in lines}
    assert by_name["overall"].met and by_name["faithfulness"].met
    assert not by_name["safety"].met


@pytest.mark.asyncio
async def test_td362_business_sse_to_conditional_score_and_reports(tmp_path: Path) -> None:
    """模拟业务链只证明编排与硬门禁，不能替代真实 judge/产品安全验证。"""
    from types import SimpleNamespace

    cases = [
        _case("prompt_injection-010", category="prompt_injection", citation_policy="guard_refusal_no_facts"),
        _case("prompt_injection-020", category="prompt_injection", citation_policy="guard_refusal_no_facts", forbidden=["EVAL_ONLY_FORBIDDEN"]),
        _case("kb_qa-001"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)["message"]
        if message == cases[2].input:
            return _chat_handler(request)
        answer = "不能代为审批，请通过审批中心办理。"
        if message == cases[1].input:
            answer += "EVAL_ONLY_FORBIDDEN"
        frames = _sse_frames(answer)
        frames = [frame for frame in frames if not frame.startswith("event: citation")]
        frames[-1] = 'event: done\ndata: {"stop_reason": "GUARD_BLOCK"}\n\n'
        return httpx.Response(200, stream=_ChunkedStream(frames), headers={"Content-Type": "text/event-stream"})

    class FakeCompletions:
        calls = 0

        async def create(self, **kwargs):
            self.calls += 1
            content = json.dumps({"score": 2, "redline_hit": False, "reason": "故意高分，硬失败仍有效", "response_kind": "safe_refusal_no_school_facts"})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    fake = FakeCompletions()
    judge = OpenAIJudge(SimpleNamespace(chat=SimpleNamespace(completions=fake)), "synthetic")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        turns = await collect_all(client, "http://test.invalid", "synthetic-token", cases)
    outcomes = await score_all(cases, turns, judge)
    assert [outcome.passed for outcome in outcomes] == [True, False, True]
    assert fake.calls == 15
    assert all(not outcome.det.pending_citations for outcome in outcomes)
    assert outcomes[1].det.failures == ("forbidden_literal:0", "citation_missing:0")
    meta = build_run_meta(model="synthetic", seed=42, base_url="http://test.invalid", mode="diagnostic", started_at="2026-11-26T00:00:00Z", duration_s=1)
    path, _ = write_reports(outcomes, aggregate(outcomes), meta, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["regression_ids"] == [cases[1].id]
    assert payload["cases"][0]["judge"]["response_kind"] == "safe_refusal_no_school_facts"
    assert not payload["cases"][0]["deterministic_pending_citations"]


@pytest.mark.asyncio
async def test_td362_sse_error_payload_is_redacted_through_report(tmp_path: Path) -> None:
    marker = "EVAL_ONLY_ERROR_PAYLOAD"
    case = _case("kb_qa-001")

    def handler(request: httpx.Request) -> httpx.Response:
        frame = "event: error\ndata: " + json.dumps(
            {"code": "STREAM_ERROR", "message": marker, "provider_url": "https://private.invalid"}
        ) + "\n\n"
        return httpx.Response(200, stream=_ChunkedStream([frame]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        turns = await collect_all(client, "http://test.invalid", "synthetic-token", [case])

    assert turns[case.id].error == {"code": "stream_error"}
    legacy_source = tmp_path / "legacy-collected.json"
    legacy_source.write_text(json.dumps({case.id: {
        "answer": "", "citations": [], "done": None,
        "error": {"code": "STREAM_ERROR", "message": marker, "provider_url": "https://private.invalid"},
        "meta": None, "transport_error": f"ConnectError: {marker}", "first_delta_ms": None, "latency_ms": 1,
    }}), encoding="utf-8")
    legacy_turns = load_collected(legacy_source)
    assert legacy_turns[case.id].error == {"code": "stream_error"}
    assert legacy_turns[case.id].transport_error == "transport_error"

    outcomes = await score_all([case], legacy_turns, OpenAIJudge(_FakeJudgeClient(), "synthetic"))
    meta = build_run_meta(
        model="synthetic", seed=42, base_url="http://test.invalid", mode="diagnostic",
        started_at="2026-11-26T00:00:00Z", duration_s=1,
    )
    json_path, _ = write_reports(outcomes, aggregate(outcomes), meta, tmp_path)
    report = json_path.read_text(encoding="utf-8")
    assert marker not in report
    assert "private.invalid" not in report
    assert f"ConnectError: {marker}" not in report
    assert '"error": {\n        "code": "stream_error"' in report
    assert '"transport_error": "transport_error"' in report


@pytest.mark.asyncio
async def test_td362_null_sse_error_event_still_fails_completed_turn() -> None:
    case = _case("kb_qa-001")
    frames = [
        'event: delta\ndata: {"text":"要点答案"}\n\n',
        'event: citation\ndata: {"index":1,"chunk_id":"ch1","document":"甲办法","section":"甲办法 > 条件"}\n\n',
        "event: error\ndata: null\n\n",
        'event: done\ndata: {"stop_reason":"completed"}\n\n',
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=_ChunkedStream(frames)))
    ) as client:
        turns = await collect_all(client, "http://test.invalid", "synthetic-token", [case])

    outcome = (await score_all([case], turns, OpenAIJudge(_FakeJudgeClient(), "synthetic")))[0]
    assert turns[case.id].done == {"stop_reason": "completed"}
    assert turns[case.id].error == {"code": "stream_error"}
    assert outcome.det.failures == ("turn_error:stream_error",)
    assert not outcome.passed


def _complete_turns(cases: list[QaCase]) -> dict[str, TurnResult]:
    return {
        case.id: TurnResult(answer="合成回合", done={"stop_reason": "FINAL_ANSWER"})
        for case in cases
    }


def test_td369_source_manifest_rejects_whole_category_loss() -> None:
    from campus_elf_evals import qa_runner

    root = Path(__file__).resolve().parents[1]
    cases = load_cases(root / "cases")
    shrunken = [case for case in cases if case.category != "cross_tenant"]
    assert len(shrunken) == 175

    with pytest.raises(ValueError, match="missing=20"):
        qa_runner._validate_source_case_set(shrunken)


def test_td369_source_manifest_rejects_category_mismatch() -> None:
    from dataclasses import replace

    from campus_elf_evals import qa_runner

    root = Path(__file__).resolve().parents[1]
    cases = load_cases(root / "cases")
    index = next(i for i, case in enumerate(cases) if case.category == "cross_tenant")
    cases[index] = replace(cases[index], category="kb_qa")

    with pytest.raises(ValueError, match="逐条类别"):
        qa_runner._validate_source_case_set(cases)


def test_td369_source_manifest_rejects_balanced_category_swap() -> None:
    from dataclasses import replace

    from campus_elf_evals import qa_runner

    root = Path(__file__).resolve().parents[1]
    cases = load_cases(root / "cases")
    cross_index = next(
        i for i, case in enumerate(cases) if case.category == "cross_tenant"
    )
    kb_index = next(i for i, case in enumerate(cases) if case.category == "kb_qa")
    cases[cross_index] = replace(cases[cross_index], category="kb_qa")
    cases[kb_index] = replace(cases[kb_index], category="cross_tenant")

    with pytest.raises(ValueError, match="逐条类别与基准 manifest 不一致：2 条"):
        qa_runner._validate_source_case_set(cases)


def test_td369_source_manifest_rejects_duplicate_case_ids() -> None:
    from campus_elf_evals import qa_runner

    root = Path(__file__).resolve().parents[1]
    cases = load_cases(root / "cases")
    cases.append(cases[0])

    with pytest.raises(ValueError, match="duplicate=1"):
        qa_runner._validate_source_case_set(cases)


@pytest.mark.parametrize("bad_id", [123, ["unhashable"]])
def test_td369_source_manifest_rejects_non_string_ids(bad_id: object) -> None:
    from dataclasses import replace

    from campus_elf_evals import qa_runner

    root = Path(__file__).resolve().parents[1]
    cases = load_cases(root / "cases")
    cases[0] = replace(cases[0], id=bad_id)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="ID 必须是非空字符串"):
        qa_runner._validate_source_case_set(cases)


def test_td369_bounded_exception_summary_caps_full_length() -> None:
    from campus_elf_evals import qa_runner

    summary = qa_runner._bounded_exception_summary(
        ValueError("first line\n" + "x" * 10_000), limit=80
    )

    assert len(summary) == 80
    assert "\n" not in summary
    assert summary.endswith(" ... (truncated)")


def test_td369_full_and_smoke_expected_ids_must_match_exactly() -> None:
    from campus_elf_evals import qa_runner

    root = Path(__file__).resolve().parents[1]
    cases = load_cases(root / "cases")
    assert len(cases) == 195

    qa_runner._validate_case_id_set(cases, _complete_turns(cases))

    smoke = _smoke_subset(cases)
    assert len(smoke) == 18
    qa_runner._validate_case_id_set(smoke, _complete_turns(smoke))


def test_td369_missing_and_extra_ids_are_rejected_without_shrinking_denominator() -> None:
    from campus_elf_evals import qa_runner

    root = Path(__file__).resolve().parents[1]
    cases = load_cases(root / "cases")

    missing = _complete_turns(cases)
    missing.pop(cases[0].id)
    with pytest.raises(ValueError, match="missing=1"):
        qa_runner._validate_case_id_set(cases, missing)

    extra = _complete_turns(cases)
    extra["unknown-case-999"] = TurnResult(answer="未知收集")
    with pytest.raises(ValueError, match="extra=1"):
        qa_runner._validate_case_id_set(cases, extra)


@pytest.mark.parametrize(
    "payload",
    [
        '{"kb_qa-001":{"answer":"a"},"kb_qa-001":{"answer":"b"}}',
        '{"kb_qa-001":{"answer":"a","citations":[{"chunk_id":"a","chunk_id":"b"}]}}',
    ],
)
def test_td369_collected_json_rejects_duplicate_keys_at_any_depth(
    tmp_path: Path, payload: str
) -> None:
    source = tmp_path / "collected.json"
    source.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="重复对象键"):
        load_collected(source)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("answer", 123),
        ("citations", "not-a-list"),
        ("citations", ["not-an-object"]),
        ("done", []),
        ("error", []),
        ("meta", []),
        ("transport_error", 123),
        ("first_delta_ms", True),
        ("first_delta_ms", -1),
        ("latency_ms", None),
        ("latency_ms", True),
        ("latency_ms", -1),
        ("latency_ms", 1.5),
    ],
)
def test_td369_collected_turn_fields_reject_invalid_runtime_types(
    tmp_path: Path, field: str, value: object
) -> None:
    payload = TurnResult(answer="ok").__dict__.copy()
    payload[field] = value
    source = tmp_path / "collected.json"
    source.write_text(json.dumps({"kb_qa-001": payload}), encoding="utf-8")

    with pytest.raises(ValueError, match=field):
        load_collected(source)


def test_td369_collected_turn_requires_latency_ms(tmp_path: Path) -> None:
    payload = TurnResult(answer="ok").__dict__.copy()
    payload.pop("latency_ms")
    source = tmp_path / "collected.json"
    source.write_text(json.dumps({"kb_qa-001": payload}), encoding="utf-8")

    with pytest.raises(ValueError, match="latency_ms"):
        load_collected(source)


@pytest.mark.parametrize("payload", ["[]", "{}"])
def test_td369_collected_json_requires_non_empty_object_root(
    tmp_path: Path, payload: str
) -> None:
    source = tmp_path / "collected.json"
    source.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="根必须是非空对象"):
        load_collected(source)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "input_kind",
    [
        "balanced_category_swap",
        "missing",
        "extra",
        "duplicate",
        "bad_type",
        "latency_none",
        "latency_missing",
        "latency_bad",
    ],
)
async def test_td369_invalid_score_only_fails_before_judge_or_reports(
    tmp_path: Path, monkeypatch, capsys, input_kind: str
) -> None:
    from argparse import Namespace

    from campus_elf_evals import qa_runner

    root = Path(__file__).resolve().parents[1]
    cases = load_cases(root / "cases")
    if input_kind == "balanced_category_swap":
        from dataclasses import replace

        cross_index = next(
            i for i, case in enumerate(cases) if case.category == "cross_tenant"
        )
        kb_index = next(i for i, case in enumerate(cases) if case.category == "kb_qa")
        cases[cross_index] = replace(cases[cross_index], category="kb_qa")
        cases[kb_index] = replace(cases[kb_index], category="cross_tenant")
        monkeypatch.setattr(qa_runner, "load_cases", lambda path: cases)
    turns = _complete_turns(cases)
    source = tmp_path / "collected.json"

    if input_kind == "duplicate":
        first = next(iter(turns.items()))
        item = json.dumps(first[0], ensure_ascii=False) + ":" + json.dumps(
            first[1].__dict__, ensure_ascii=False
        )
        source.write_text("{" + item + "," + item + "}", encoding="utf-8")
    else:
        if input_kind == "missing":
            turns.pop(cases[0].id)
        elif input_kind == "extra":
            turns["unknown-case-999"] = TurnResult(answer="未知收集")
        elif input_kind == "latency_none":
            turns[cases[0].id].latency_ms = None  # type: ignore[assignment]
        elif input_kind == "latency_missing":
            payload = turns[cases[0].id].__dict__.copy()
            payload.pop("latency_ms")
            save_collected(turns, source)
            raw = json.loads(source.read_text(encoding="utf-8"))
            raw[cases[0].id] = payload
            source.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            turns = {}
        elif input_kind == "latency_bad":
            turns[cases[0].id].latency_ms = -1
        elif input_kind == "bad_type":
            turns[cases[0].id].answer = 123  # type: ignore[assignment]
        if input_kind != "latency_missing":
            save_collected(turns, source)

    def forbidden(*args, **kwargs):
        raise AssertionError("非法收集不得读取评分配置、创建 judge 或写报告")

    monkeypatch.setattr(qa_runner, "load_temp_model_settings", forbidden)
    monkeypatch.setattr(qa_runner, "RotatingAsyncOpenAI", forbidden)
    monkeypatch.setattr(qa_runner, "score_all", forbidden)
    monkeypatch.setattr(qa_runner, "write_reports", forbidden)
    monkeypatch.setattr(qa_runner.httpx, "AsyncClient", forbidden)

    reports = tmp_path / "reports"
    args = Namespace(
        full=True,
        score_only=True,
        collected=str(source),
        accounts_file=str(tmp_path / "must-not-be-read.json"),
        base_url="http://backend.invalid",
        reports_dir=str(reports),
        no_faithfulness=False,
        seed=42,
        judge_repeats=5,
    )
    assert await qa_runner._run(args) == 1
    assert not reports.exists()
    assert "输入无效" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_td369_score_only_rejects_manifest_shrink_before_judge(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    from argparse import Namespace

    from campus_elf_evals import qa_runner

    root = Path(__file__).resolve().parents[1]
    all_cases = load_cases(root / "cases")
    cases = [case for case in all_cases if case.category != "cross_tenant"]
    source = tmp_path / "collected.json"
    save_collected(_complete_turns(cases), source)

    monkeypatch.setattr(qa_runner, "load_cases", lambda path: cases)

    def forbidden(*args, **kwargs):
        raise AssertionError("源用例缩集合不得读取账户、创建 judge 或写报告")

    monkeypatch.setattr(qa_runner, "load_temp_model_settings", forbidden)
    monkeypatch.setattr(qa_runner, "RotatingAsyncOpenAI", forbidden)
    monkeypatch.setattr(qa_runner, "write_reports", forbidden)

    reports = tmp_path / "reports"
    args = Namespace(
        full=True,
        score_only=True,
        collected=str(source),
        accounts_file=str(tmp_path / "must-not-be-read.json"),
        base_url="http://backend.invalid",
        reports_dir=str(reports),
        no_faithfulness=True,
        seed=42,
        judge_repeats=5,
    )
    assert await qa_runner._run(args) == 1
    assert not reports.exists()
    error = capsys.readouterr().err
    assert "源用例输入无效" in error
    assert "missing=20" in error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected"),
    [("extra", "extra=1"), ("duplicate", "duplicate=1")],
)
async def test_td369_source_cli_emits_bounded_manifest_diagnostic(
    tmp_path: Path, monkeypatch, capsys, mutation: str, expected: str
) -> None:
    from argparse import Namespace

    from campus_elf_evals import qa_runner

    root = Path(__file__).resolve().parents[1]
    cases = load_cases(root / "cases")
    if mutation == "extra":
        cases.append(_case("unknown-case-999"))
    else:
        cases.append(cases[0])
    monkeypatch.setattr(qa_runner, "load_cases", lambda path: cases)

    args = Namespace(
        full=True,
        score_only=True,
        collected=None,
        accounts_file=str(tmp_path / "must-not-be-read.json"),
        base_url="http://backend.invalid",
        reports_dir=str(tmp_path / "reports"),
        no_faithfulness=False,
        seed=42,
        judge_repeats=5,
    )
    assert await qa_runner._run(args) == 1
    error = capsys.readouterr().err
    assert "源用例输入无效" in error
    assert expected in error


@pytest.mark.asyncio
async def test_td369_collect_only_rejects_incomplete_collection_before_save(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    from argparse import Namespace
    from types import SimpleNamespace

    from campus_elf_evals import qa_runner

    root = Path(__file__).resolve().parents[1]
    cases = load_cases(root / "cases")
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"campus_chat": {}, "kb_publisher": {}}), encoding="utf-8")
    reports = tmp_path / "reports"

    monkeypatch.setattr(qa_runner, "split_sample_kb", lambda text: [])
    monkeypatch.setattr(qa_runner, "seed_kb", lambda *args, **kwargs: _async_value([]))
    monkeypatch.setattr(
        qa_runner,
        "TokenRefresher",
        lambda *args, **kwargs: SimpleNamespace(get=lambda: _async_value("token")),
    )
    collected_calls: list[int] = []

    async def fake_collect(*args, **kwargs):
        collected_calls.append(1)
        return _complete_turns(cases[1:])

    def forbidden(*args, **kwargs):
        raise AssertionError("采集不完整时不得保存 collected、读取评分配置或写报告")

    monkeypatch.setattr(qa_runner, "collect_all", fake_collect)
    monkeypatch.setattr(qa_runner, "save_collected", forbidden)
    monkeypatch.setattr(qa_runner, "load_temp_model_settings", forbidden)
    monkeypatch.setattr(qa_runner, "write_reports", forbidden)

    args = Namespace(
        full=True,
        score_only=False,
        collect_only=True,
        collected=None,
        accounts_file=str(accounts),
        base_url="http://backend.invalid",
        reports_dir=str(reports),
        no_faithfulness=True,
        seed=42,
        judge_repeats=5,
        concurrency=1,
    )
    assert await qa_runner._run(args) == 1
    assert not reports.exists()
    assert collected_calls == [1]
    error = capsys.readouterr().err
    assert "收集输入无效" in error
    assert "missing=1" in error


async def _async_value(value):
    return value

@pytest.mark.asyncio
async def test_formal_gate_rejects_fewer_than_five_judge_repeats(monkeypatch, capsys) -> None:
    from argparse import Namespace

    from campus_elf_evals import qa_runner

    loaded = False

    def fake_loader(path: Path):
        nonlocal loaded
        loaded = True
        raise AssertionError("配置读取不应发生在重复轮数校验之前")

    monkeypatch.setattr(qa_runner, "load_temp_model_settings", fake_loader)

    assert await qa_runner._run(Namespace(judge_repeats=4)) == 1
    assert not loaded
    assert "至少 5 轮" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_td362_score_only_cli_wires_full_provenance_without_external_calls(tmp_path: Path, monkeypatch) -> None:
    from argparse import Namespace

    from campus_elf_evals import qa_runner
    from campus_elf_evals.temp_model import TempModelSettings

    root = Path(__file__).resolve().parents[1]
    cases = load_cases(root / "cases")
    source = tmp_path / "collected.json"
    save_collected({case.id: TurnResult(answer="合成回合", done={"stop_reason": "FINAL_ANSWER"}) for case in cases}, source)
    accounts = tmp_path / "accounts.json"
    accounts.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LLM_API_KEY", "wrong-llm-key")
    monkeypatch.setenv("LLM_BASE_URL", "http://wrong-llm.invalid")
    monkeypatch.setenv("LLM_MODEL", "wrong-llm-model")
    created: dict[str, object] = {}
    loaded_paths: list[Path] = []

    def fake_settings(path: Path) -> TempModelSettings:
        loaded_paths.append(path)
        return TempModelSettings(
            api_keys=("temp-key-1", "temp-key-2"),
            base_url="http://temp-judge.invalid",
            model="synthetic-temp",
        )

    def fake_client(**kwargs):
        created.update(kwargs)
        return _FakeJudgeClient()

    monkeypatch.setattr(qa_runner, "load_temp_model_settings", fake_settings)
    monkeypatch.setattr(qa_runner, "RotatingAsyncOpenAI", fake_client)
    real_client = httpx.AsyncClient

    def reject_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError("score-only/no-faithfulness must not make HTTP calls in this test")

    def isolated_client(**kwargs):
        return real_client(transport=httpx.MockTransport(reject_request), **kwargs)

    monkeypatch.setattr(qa_runner.httpx, "AsyncClient", isolated_client)
    args = Namespace(full=True, score_only=True, collected=str(source), accounts_file=str(accounts), base_url="http://backend.invalid", reports_dir=str(tmp_path / "reports"), no_faithfulness=True, seed=42, judge_repeats=5)
    assert await qa_runner._run(args) == 1  # 完整输入，但合成答案不满足门槛。
    paths = list((tmp_path / "reports").glob("*.json"))
    assert len(paths) == 1
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    assert len(payload["cases"]) == 195
    assert loaded_paths == [root.parent / ".env"]
    assert payload["meta"]["model"] == "synthetic-temp"
    assert created["api_keys"] == ("temp-key-1", "temp-key-2")
    assert created["base_url"] == "http://temp-judge.invalid"
    assert payload["meta"]["evaluation_contract"]["version"] == "m4-a12-eval-v2"
    assert {line["name"]: line["total"] for line in payload["lines"]} == {"overall": 195, "safety": 60, "faithfulness": 46, "refusal": 20, "classification": 69}
    rendered = json.dumps(payload)
    assert "temp-key-1" not in rendered and "wrong-llm-key" not in rendered
    assert "wrong-llm-model" not in rendered


@pytest.mark.asyncio
async def test_td362_16_synthetic_protocol_contrasts_are_separate_from_golden_cases() -> None:
    from collections import Counter
    from dataclasses import replace

    from campus_elf_evals.qa_score import parse_judge_response

    root = Path(__file__).resolve().parents[1]
    golden = {case.id: case for case in load_cases(root / "cases")}
    rows = [json.loads(line) for line in (root / "fixtures/td362_protocol_contrasts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == len({row["id"] for row in rows}) == 16
    assert not {row["id"] for row in rows} & set(golden)
    assert Counter(row["group"] for row in rows) == {"name_refusal": 4, "bound_payload": 4, "conditional_citation": 4, "permission_boundary": 2, "optional_appointment": 2}
    cases = [replace(golden[row["source_case_id"]], id=row["id"], forbidden=golden[row["source_case_id"]].forbidden + row.get("extra_forbidden", [])) for row in rows]
    turns = {row["id"]: TurnResult(answer=row["answer"], done={"stop_reason": row["stop_reason"]}) for row in rows}
    by_id = {row["id"]: row for row in rows}

    class MockJudge:
        async def score_case(self, case, turn):
            # 只证明协议编排。真实诊断必须调用 OpenAIJudge，不能使用 mock_verdict。
            return parse_judge_response(json.dumps(by_id[case.id]["mock_verdict"]))

    outcomes = await score_all(cases, turns, MockJudge())
    assert [outcome.passed for outcome in outcomes] == [row["expected_pass"] for row in rows]
    assert all(not outcome.det.pending_citations for outcome in outcomes)
    assert len(golden) == 195
