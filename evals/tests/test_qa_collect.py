"""qa_collect 单测：SSE 增量解析 + HTTP 收集器（M4-EVAL 简报步 2）。

判例（UI2）：mock 必须真分块——本文件的 fake 流按帧分片喂入，
且断言 delta 是逐段拼接而非一次性全文。
"""

from __future__ import annotations

import json

import httpx
import pytest

from campus_elf_evals.qa_collect import SSEParser, TurnResult, collect_turn


def _frame(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def test_parser_incremental_frames() -> None:
    parser = SSEParser()
    assert parser.feed("event: delta") == []
    assert parser.feed('data: {"text":"甲"}') == []
    assert parser.feed("") == [("delta", {"text": "甲"})]
    assert parser.feed("event: delta") == []
    assert parser.feed('data: {"text":"乙"}') == []
    assert parser.feed("") == [("delta", {"text": "乙"})]


def test_parser_ignores_heartbeat_and_unknown_lines() -> None:
    parser = SSEParser()
    assert parser.feed(": keep-alive") == []
    assert parser.feed("") == []
    assert parser.feed("retry: 3000") == []
    assert parser.feed("event: done") == []
    assert parser.feed('data: {"stop_reason":"completed"}') == []
    assert parser.feed("") == [("done", {"stop_reason": "completed"})]


def test_parser_rejects_malformed_data() -> None:
    parser = SSEParser()
    parser.feed("event: delta")
    parser.feed("data: {不是json")
    with pytest.raises(ValueError):
        parser.feed("")


class _ChunkedStream(httpx.AsyncByteStream):
    """真分块投递：每帧一个 chunk（UI2 判例②——一次性全文证明不了流式）。"""

    def __init__(self, frames: list[str]) -> None:
        self._frames = frames

    async def __aiter__(self):
        for frame in self._frames:
            yield frame.encode("utf-8")


def _stream_response(frames: list[str]) -> httpx.Response:
    return httpx.Response(200, stream=_ChunkedStream(frames))


@pytest.mark.asyncio
async def test_collect_turn_happy_path() -> None:
    frames = [
        _frame("meta", {"turn_id": "t1", "trace_id": "tr1", "conversation_id": "c1"}),
        _frame("step", {"stage": "route", "label": "正在理解问题"}),
        _frame("delta", {"text": "甲"}),
        _frame("delta", {"text": "乙"}),
        _frame("citation", {"index": 1, "chunk_id": "ch1", "document": "奖学金评定办法", "page": None, "section": "奖学金评定办法 > 申请条件"}),
        _frame("done", {"stop_reason": "completed", "tokens_in": 10, "tokens_out": 5}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/campus/chat/turns"
        assert request.headers["Authorization"] == "Bearer tok"
        assert json.loads(request.content)["message"] == "问题"
        return _stream_response(frames)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collect_turn(client, base_url="http://x", token="tok", message="问题")
    assert isinstance(result, TurnResult)
    assert result.answer == "甲乙"
    assert result.citations[0]["chunk_id"] == "ch1"
    assert result.done is not None and result.done["stop_reason"] == "completed"
    assert result.error is None and result.transport_error is None
    assert result.meta is not None and result.meta["trace_id"] == "tr1"


@pytest.mark.asyncio
async def test_collect_turn_error_event() -> None:
    frames = [
        _frame("meta", {"turn_id": "t1", "trace_id": "tr1", "conversation_id": "c1"}),
        _frame("error", {"code": "LLM_NOT_CONFIGURED", "retryable": False}),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: _stream_response(frames))
    ) as client:
        result = await collect_turn(client, base_url="http://x", token="tok", message="问题")
    assert result.error is not None and result.error["code"] == "LLM_NOT_CONFIGURED"
    assert result.done is None


@pytest.mark.asyncio
async def test_collect_turn_http_401_no_retry() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"detail": "unauthorized"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collect_turn(client, base_url="http://x", token="tok", message="问题")
    assert result.transport_error is not None and "401" in result.transport_error
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_collect_turn_transport_error_retries_once() -> None:
    calls = []
    frames = [_frame("done", {"stop_reason": "completed"})]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom", request=request)
        return _stream_response(frames)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collect_turn(client, base_url="http://x", token="tok", message="问题")
    assert len(calls) == 2
    assert result.done is not None and result.transport_error is None


@pytest.mark.asyncio
async def test_collect_turn_transport_error_gives_up_after_two() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("boom", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collect_turn(client, base_url="http://x", token="tok", message="问题")
    assert len(calls) == 2
    assert result.transport_error is not None
    assert result.done is None