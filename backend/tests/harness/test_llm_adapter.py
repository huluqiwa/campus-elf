"""OpenAI 兼容适配器校验（M4-AI01a 步 3/4/6）。

全部离线：httpx.MockTransport 脚本化 provider 响应；重试退避用注入 sleep 记录，
不 sleep 真实时间。覆盖：complete/stream 映射、A8 重试语义（首 token 前 ≤2 次、
流中断不重试）、错误码全映射矩阵、熔断联动、脱敏反证（key/provider 原文不出圈）。
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from app.harness.llm import (
    LlmCircuitBreaker,
    LlmError,
    LlmErrorCode,
    LlmRequest,
    LlmStreamEvent,
    OpenAICompatLlm,
)
from tests.support.llm import FAKE_LLM_API_KEY

BASE_URL = "https://llm.test/v1"
MODEL = "test-model"

ScriptItem = httpx.Response | BaseException  # CancelledError 是 BaseException 而非 Exception


class _Scripted:
    """按脚本弹出响应/异常的 MockTransport 处理器（记录每次请求）。"""

    def __init__(self, script: list[ScriptItem]) -> None:
        self.script = script
        self.calls: list[httpx.Request] = []
        self.http: httpx.AsyncClient | None = None  # 由 scripted_adapter 注入

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _SleepRecorder:
    """记录退避时长（不真实睡眠）。"""

    def __init__(self) -> None:
        self.durations: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)


def completion_json(content: str | None = "回答文本") -> dict[str, Any]:
    """构造 chat.completion 响应体（带 usage）。"""

    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1700000000,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 300, "total_tokens": 420},
    }


def sse_chunk(
    *,
    content: str | None = None,
    finish_reason: str | None = None,
    usage: dict[str, int] | None = None,
) -> str:
    """构造 chat.completion.chunk 的 SSE 行（usage chunk 的 choices 为空数组）。"""

    payload: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": MODEL,
        "choices": [],
    }
    if content is not None or finish_reason is not None:
        payload["choices"] = [
            {
                "index": 0,
                "delta": {"content": content} if content is not None else {},
                "finish_reason": finish_reason,
            }
        ]
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


SSE_DONE = "data: [DONE]\n\n"


def sse_response(lines: list[str]) -> httpx.Response:
    """整段 SSE 文本的 200 响应。"""

    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join(lines).encode("utf-8"),
    )


def sse_streaming_response(items: list[str | BaseException]) -> httpx.Response:
    """逐块产出的流式响应（异常项在块间抛出，模拟流中断）。"""

    async def byte_stream() -> AsyncIterator[bytes]:
        for item in items:
            if isinstance(item, BaseException):
                raise item
            yield item.encode("utf-8")

    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=byte_stream(),
    )


@asynccontextmanager
async def scripted_adapter(
    script: list[ScriptItem],
    *,
    breaker: LlmCircuitBreaker | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    **overrides: Any,
) -> AsyncIterator[tuple[OpenAICompatLlm, _Scripted]]:
    """构造注入 MockTransport 的适配器（结束统一关闭客户端）。"""

    scripted = _Scripted(script)
    http = httpx.AsyncClient(transport=httpx.MockTransport(scripted.handler))
    scripted.http = http
    client = AsyncOpenAI(
        api_key=FAKE_LLM_API_KEY,
        base_url=BASE_URL,
        http_client=http,
        max_retries=0,  # 重试语义归适配器（SDK 内建重试必须关闭，否则计数/熔断失真）
    )
    adapter = OpenAICompatLlm(
        api_key=SecretStr(FAKE_LLM_API_KEY),
        base_url=BASE_URL,
        model=MODEL,
        client=client,
        breaker=breaker,
        sleep=sleep if sleep is not None else _SleepRecorder(),
        **overrides,
    )
    try:
        yield adapter, scripted
    finally:
        await client.close()


def chat_request() -> LlmRequest:
    """最小对话请求。"""

    return LlmRequest(messages=[{"role": "user", "content": "你好"}])


# ---------- complete ----------


async def test_complete_maps_text_usage_and_finish() -> None:
    """200 响应映射：text/finish_reason/tokens/model 全部落位。"""

    async with scripted_adapter([httpx.Response(200, json=completion_json())]) as (
        adapter,
        scripted,
    ):
        response = await adapter.complete(chat_request())
    assert response.text == "回答文本"
    assert response.finish_reason == "stop"
    assert response.tokens_in == 120
    assert response.tokens_out == 300
    assert response.model == MODEL
    assert response.latency_ms >= 0
    assert len(scripted.calls) == 1


async def test_complete_without_usage_keeps_none() -> None:
    """provider 未报 usage 时保持 None（不伪造）。"""

    body = completion_json()
    body.pop("usage")
    async with scripted_adapter([httpx.Response(200, json=body)]) as (adapter, _):
        response = await adapter.complete(chat_request())
    assert response.tokens_in is None
    assert response.tokens_out is None


async def test_complete_maps_tool_calls_and_empty_text() -> None:
    """tool_calls 透传为 dict 元组；content 为空时 text 为空串。"""

    body = completion_json(content=None)
    body["choices"][0]["message"]["tool_calls"] = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "kb.search", "arguments": '{"query":"校历"}'},
        }
    ]
    body["choices"][0]["finish_reason"] = "tool_calls"
    async with scripted_adapter([httpx.Response(200, json=body)]) as (adapter, _):
        response = await adapter.complete(chat_request())
    assert response.text == ""
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0]["function"]["name"] == "kb.search"


async def test_auth_error_is_not_retryable() -> None:
    """401 → LLM_AUTH，不重试（只发一次请求）。"""

    async with scripted_adapter(
        [httpx.Response(401, json={"error": {"message": "invalid key"}})]
    ) as (adapter, scripted):
        with pytest.raises(LlmError) as excinfo:
            await adapter.complete(chat_request())
    assert excinfo.value.code is LlmErrorCode.AUTH
    assert excinfo.value.retryable is False
    assert len(scripted.calls) == 1


async def test_rate_limit_retries_with_backoff_then_succeeds() -> None:
    """429 → 退避重试 ≤2 次后成功；退避按指数基数记录。"""

    sleep = _SleepRecorder()
    async with scripted_adapter(
        [
            httpx.Response(429, json={"error": {"message": "slow down"}}),
            httpx.Response(429, json={"error": {"message": "slow down"}}),
            httpx.Response(200, json=completion_json()),
        ],
        sleep=sleep,
    ) as (adapter, scripted):
        response = await adapter.complete(chat_request())
    assert response.text == "回答文本"
    assert len(scripted.calls) == 3
    assert sleep.durations == [0.5, 1.0]


async def test_rate_limit_exhausted_raises() -> None:
    """429 三连 → LLM_RATE_LIMITED（retryable=True），恰好 3 次请求。"""

    async with scripted_adapter(
        [httpx.Response(429, json={"error": {"message": "slow down"}}) for _ in range(3)]
    ) as (adapter, scripted):
        with pytest.raises(LlmError) as excinfo:
            await adapter.complete(chat_request())
    assert excinfo.value.code is LlmErrorCode.RATE_LIMITED
    assert excinfo.value.retryable is True
    assert len(scripted.calls) == 3


async def test_server_error_retries_then_succeeds() -> None:
    """5xx → 可重试，恢复后成功。"""

    async with scripted_adapter(
        [
            httpx.Response(500, json={"error": {"message": "boom"}}),
            httpx.Response(200, json=completion_json()),
        ]
    ) as (adapter, scripted):
        response = await adapter.complete(chat_request())
    assert response.text == "回答文本"
    assert len(scripted.calls) == 2


async def test_bad_request_is_not_retryable() -> None:
    """400 → LLM_PROVIDER_ERROR 且不重试（语义错误不会自愈）。"""

    async with scripted_adapter(
        [httpx.Response(400, json={"error": {"message": "bad schema"}})]
    ) as (adapter, scripted):
        with pytest.raises(LlmError) as excinfo:
            await adapter.complete(chat_request())
    assert excinfo.value.code is LlmErrorCode.PROVIDER_ERROR
    assert excinfo.value.retryable is False
    assert len(scripted.calls) == 1


async def test_connect_timeout_retries_then_succeeds() -> None:
    """连接超时 → 首 token 前可重试，第二次成功。"""

    async with scripted_adapter(
        [httpx.ConnectTimeout("connect timed out"), httpx.Response(200, json=completion_json())]
    ) as (adapter, scripted):
        response = await adapter.complete(chat_request())
    assert response.text == "回答文本"
    assert len(scripted.calls) == 2


async def test_connect_timeout_exhausted_raises_llm_timeout() -> None:
    """连接超时三连 → LLM_TIMEOUT（retryable=True）。"""

    async with scripted_adapter(
        [httpx.ConnectTimeout("connect timed out") for _ in range(3)]
    ) as (adapter, scripted):
        with pytest.raises(LlmError) as excinfo:
            await adapter.complete(chat_request())
    assert excinfo.value.code is LlmErrorCode.TIMEOUT
    assert excinfo.value.retryable is True
    assert len(scripted.calls) == 3


# ---------- stream ----------


async def test_stream_maps_events_in_order() -> None:
    """事件顺序：delta* → finish → usage（include_usage 末 chunk 时序钉住）。"""

    async with scripted_adapter(
        [
            sse_response(
                [
                    sse_chunk(content="根据"),
                    sse_chunk(content="校历"),
                    sse_chunk(finish_reason="stop"),
                    sse_chunk(usage={"prompt_tokens": 1200, "completion_tokens": 300}),
                    SSE_DONE,
                ]
            )
        ]
    ) as (adapter, scripted):
        events = [event async for event in adapter.stream(chat_request())]
    assert [event.kind for event in events] == ["delta", "delta", "finish", "usage"]
    assert events[0].text_delta == "根据"
    assert events[1].text_delta == "校历"
    assert events[2].finish_reason == "stop"
    assert events[3].tokens_in == 1200
    assert events[3].tokens_out == 300
    assert len(scripted.calls) == 1


async def test_stream_retries_before_first_token() -> None:
    """首 token 前失败可重试：消费方只看到成功尝试的事件。"""

    async with scripted_adapter(
        [
            httpx.ConnectTimeout("connect timed out"),
            sse_response([sse_chunk(content="答案"), sse_chunk(finish_reason="stop"), SSE_DONE]),
        ]
    ) as (adapter, scripted):
        events = [event async for event in adapter.stream(chat_request())]
    assert [event.kind for event in events] == ["delta", "finish"]
    assert events[0].text_delta == "答案"
    assert len(scripted.calls) == 2


async def test_stream_midstream_failure_raises_non_retryable() -> None:
    """流中断不重试：已产出 delta 后超时 → LLM_TIMEOUT retryable=False，只发一次请求。"""

    async with scripted_adapter(
        [
            sse_streaming_response(
                [sse_chunk(content="部分"), httpx.ReadTimeout("read timed out"), SSE_DONE]
            )
        ]
    ) as (adapter, scripted):
        collected: list[LlmStreamEvent] = []
        with pytest.raises(LlmError) as excinfo:
            async for event in adapter.stream(chat_request()):
                collected.append(event)
    assert [event.text_delta for event in collected] == ["部分"]
    assert excinfo.value.code is LlmErrorCode.TIMEOUT
    assert excinfo.value.retryable is False
    assert excinfo.value.__context__ is None  # stream 侧同样不滞留 provider 异常（复审 m1）
    assert len(scripted.calls) == 1


async def test_stream_midstream_failures_drive_breaker_open() -> None:
    """流中断计入熔断：5 次后开路，第 6 次调用不触网（calls 不再增长）。"""

    breaker = LlmCircuitBreaker(clock=lambda: 0.0)
    script: list[ScriptItem] = [
        sse_streaming_response([sse_chunk(content="x"), httpx.ReadTimeout("boom"), SSE_DONE])
        for _ in range(5)
    ]
    async with scripted_adapter(script, breaker=breaker) as (adapter, scripted):
        for _ in range(5):
            with pytest.raises(LlmError):
                collected = [event async for event in adapter.stream(chat_request())]
                assert collected  # 每次都先收到 delta 再中断
        assert breaker.state == "open"
        with pytest.raises(LlmError) as excinfo:
            [event async for event in adapter.stream(chat_request())]
    assert excinfo.value.code is LlmErrorCode.BREAKER_OPEN
    assert len(scripted.calls) == 5


async def test_breaker_open_short_circuits_network() -> None:
    """开路期间 complete 不发起网络请求（calls == 0）。"""

    breaker = LlmCircuitBreaker(clock=lambda: 0.0)
    for _ in range(5):
        breaker.record_failure()
    async with scripted_adapter([httpx.Response(200, json=completion_json())], breaker=breaker) as (
        adapter,
        scripted,
    ):
        with pytest.raises(LlmError) as excinfo:
            await adapter.complete(chat_request())
    assert excinfo.value.code is LlmErrorCode.BREAKER_OPEN
    assert len(scripted.calls) == 0


async def test_stream_breaker_open_short_circuits_network() -> None:
    """开路期间 stream 同样短路。"""

    breaker = LlmCircuitBreaker(clock=lambda: 0.0)
    for _ in range(5):
        breaker.record_failure()
    async with scripted_adapter([httpx.Response(200, json=completion_json())], breaker=breaker) as (
        adapter,
        scripted,
    ):
        with pytest.raises(LlmError) as excinfo:
            [event async for event in adapter.stream(chat_request())]
    assert excinfo.value.code is LlmErrorCode.BREAKER_OPEN
    assert len(scripted.calls) == 0


# ---------- 请求体接线（审查 m4） ----------


async def test_request_body_carries_overrides_and_tools() -> None:
    """请求体按契约组装：model/temperature/max_tokens 覆盖与 tools 透传落位。"""

    async with scripted_adapter([httpx.Response(200, json=completion_json())]) as (
        adapter,
        scripted,
    ):
        request = LlmRequest(
            messages=[{"role": "user", "content": "你好"}],
            model="override-model",
            temperature=0.3,
            max_tokens=128,
            tool_specs=(
                {
                    "type": "function",
                    "function": {"name": "kb.search", "description": "检索", "parameters": {}},
                },
            ),
        )
        await adapter.complete(request)
    body = json.loads(scripted.calls[0].content.decode("utf-8"))
    assert body["model"] == "override-model"
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == 128
    assert body["tools"][0]["function"]["name"] == "kb.search"


async def test_aclose_closes_underlying_client() -> None:
    """shutdown 钩子语义：aclose 关闭底层 httpx 客户端（审查 m7）。"""

    async with scripted_adapter([]) as (adapter, scripted):
        http = scripted.http
        assert http is not None and not http.is_closed
        await adapter.aclose()
        assert http is not None and http.is_closed
        # 关闭后调用：SDK 把已关闭客户端的 RuntimeError 包装为 APIConnectionError
        # → 适配器映射 LLM_PROVIDER_ERROR（客户端不可再用即成立）。
        with pytest.raises(LlmError) as excinfo:
            await adapter.complete(chat_request())
    assert excinfo.value.code is LlmErrorCode.PROVIDER_ERROR


# ---------- 取消路径释放试探名额（审查 M1） ----------


class _MutableClock:
    """可推进的时钟（适配器级熔断联动用例）。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_cancelled_call_releases_half_open_trial() -> None:
    """半开试探被取消：名额回收且不计失败；下一次调用重新试探并成功（审查 M1）。"""

    clock = _MutableClock()
    breaker = LlmCircuitBreaker(clock=clock)
    for _ in range(5):
        breaker.record_failure()  # now=0 → 开路
    clock.now = 1000.0  # 开路期满
    async with scripted_adapter(
        [
            asyncio.CancelledError(),  # 试探被取消：走 BaseException 释放路径
            httpx.Response(200, json=completion_json()),  # 重新试探成功
        ],
        breaker=breaker,
    ) as (adapter, scripted):
        with pytest.raises(asyncio.CancelledError):
            await adapter.complete(chat_request())
        open_state: str = breaker.state  # 注解为 str：断开 mypy 属性收窄
        assert open_state == "open"  # 取消不记失败、不开新窗口
        response = await adapter.complete(chat_request())  # 重新试探
    assert response.text == "回答文本"
    closed_state: str = breaker.state
    assert closed_state == "closed"
    assert len(scripted.calls) == 2


async def test_stream_cancelled_midstream_releases_half_open_trial() -> None:
    """stream 侧同名路径（复审 m1）：半开试探在流中段被取消→名额回收→重探成功。"""

    clock = _MutableClock()
    breaker = LlmCircuitBreaker(clock=clock)
    for _ in range(5):
        breaker.record_failure()
    clock.now = 1000.0
    async with scripted_adapter(
        [
            sse_streaming_response(
                [sse_chunk(content="x"), asyncio.CancelledError(), SSE_DONE]
            ),
            sse_response([sse_chunk(content="答案"), sse_chunk(finish_reason="stop"), SSE_DONE]),
        ],
        breaker=breaker,
    ) as (adapter, scripted):
        with pytest.raises(asyncio.CancelledError):
            [event async for event in adapter.stream(chat_request())]
        open_state: str = breaker.state
        assert open_state == "open"  # 取消不记失败
        events = [event async for event in adapter.stream(chat_request())]  # 重新试探
    assert [event.kind for event in events] == ["delta", "finish"]
    closed_state: str = breaker.state
    assert closed_state == "closed"
    assert len(scripted.calls) == 2


# ---------- 脱敏反证（简报 A3/A4） ----------


async def test_error_never_carries_provider_payload_or_key() -> None:
    """异常文本/args/cause/__context__ 均不含 provider 响应原文与 API key。"""

    provider_text = "invalid api key for tenant secret-org"
    async with scripted_adapter(
        [httpx.Response(401, json={"error": {"message": provider_text}})]
    ) as (adapter, _):
        with pytest.raises(LlmError) as excinfo:
            await adapter.complete(chat_request())
    error = excinfo.value
    rendered = f"{error} | {error.args!r}"
    assert FAKE_LLM_API_KEY not in rendered
    assert provider_text not in rendered
    assert error.__cause__ is None  # 异常链不携带 provider 原始异常
    # 审查 M2：`from None` 只清显示，__context__ 仍指向原始异常（其
    # request/response 链含 Authorization key）——必须为 None。
    assert error.__context__ is None


async def test_failures_do_not_log_key_or_provider_text(caplog: pytest.LogCaptureFixture) -> None:
    """失败日志只含内部错误码与状态码：key/provider 原文零出现。"""

    provider_text = "quota exceeded for org super-secret-org"
    with caplog.at_level(logging.WARNING, logger="app.harness.llm"):
        async with scripted_adapter(
            [
                httpx.Response(429, json={"error": {"message": provider_text}}),
                httpx.Response(429, json={"error": {"message": provider_text}}),
                httpx.Response(429, json={"error": {"message": provider_text}}),
            ]
        ) as (adapter, _):
            with pytest.raises(LlmError):
                await adapter.complete(chat_request())
    assert FAKE_LLM_API_KEY not in caplog.text
    assert provider_text not in caplog.text
    assert "LLM_RATE_LIMITED" in caplog.text
    assert "429" in caplog.text
