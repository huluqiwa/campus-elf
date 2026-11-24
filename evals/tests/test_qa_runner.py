"""qa_runner 集成测试（fake SSE/judge/faithfulness，CI 可跑）：
收集并发 / collected 持久化往返 / 评分编排 / 报告落盘。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from campus_elf_evals.qa_cases import QaCase
from campus_elf_evals.qa_collect import TurnResult
from campus_elf_evals.qa_report import aggregate, build_run_meta, write_reports
from campus_elf_evals.qa_runner import (
    _smoke_subset,
    collect_all,
    load_collected,
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
    # 单条评估故障：如实记录，不拖垮整批、不偷偷改判 passed
    assert by_id[boom.id].faithfulness is None
    assert "ragas 故障" in (by_id[boom.id].faithfulness_error or "")
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