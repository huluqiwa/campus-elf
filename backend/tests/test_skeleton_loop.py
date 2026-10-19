"""空壳 Loop SSE 测试（M0-F）。

- 开关：`DEV_SKELETON_LOOP_ENABLED`（默认 false，关闭时 404）；
- 事件字段与 `docs/design.md` §5.8 契约对齐；
- trace sink 注入内存实现，断言「本地 trace 必发」（含中断路径）与不含消息原文。
"""

import json
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request as StarletteRequest

from app.api.campus_chat import ChatTurnRequest, create_chat_turn
from app.config import get_settings
from app.harness.contracts import StopReason
from app.harness.trace import InMemoryTraceSink
from app.main import create_app

ENDPOINT = "/api/v1/campus/chat/turns"


def parse_sse(body: str) -> list[tuple[str, dict[str, object]]]:
    """把 SSE 响应体解析为 (事件名, 负载) 列表。"""

    events: list[tuple[str, dict[str, object]]] = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event_name = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        events.append((event_name, json.loads(data)))
    return events


def make_request(app: Any, *, path: str = ENDPOINT) -> StarletteRequest:
    """构造可直接调用路由函数的 Starlette Request。"""

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 1234),
        "root_path": "",
        "http_version": "1.1",
        "app": app,
    }
    return StarletteRequest(scope)


@pytest.fixture
def sink() -> InMemoryTraceSink:
    """注入的 trace sink。"""

    return InMemoryTraceSink()


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """用例前后清配置缓存，保证环境变量覆盖生效且不泄漏。"""

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def disabled_client(sink: InMemoryTraceSink) -> AsyncIterator[AsyncClient]:
    """开关关闭的应用客户端（生产默认形态）。"""

    transport = ASGITransport(app=create_app(trace_sink=sink))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def enabled_client(
    monkeypatch: pytest.MonkeyPatch, sink: InMemoryTraceSink
) -> AsyncIterator[AsyncClient]:
    """开关打开的应用客户端（开发/测试形态）。"""

    monkeypatch.setenv("DEV_SKELETON_LOOP_ENABLED", "true")
    get_settings.cache_clear()
    transport = ASGITransport(app=create_app(trace_sink=sink))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_disabled_returns_404_without_trace(
    disabled_client: AsyncClient, sink: InMemoryTraceSink
) -> None:
    """开关关闭时返回 404，且不产生 trace。"""

    response = await disabled_client.post(ENDPOINT, json={"message": "你好"})

    assert response.status_code == 404
    assert sink.turns == []


@pytest.mark.asyncio
async def test_event_contract_and_stop_reason(
    enabled_client: AsyncClient, sink: InMemoryTraceSink
) -> None:
    """事件序列为 meta → step* → done；字段与 §5.8 契约一致；响应头关闭缓冲。"""

    response = await enabled_client.post(
        ENDPOINT, json={"message": "图书馆几点开门", "conversation_id": "conv-1"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"

    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[0] == "meta"
    assert names[-1] == "done"
    assert all(name == "step" for name in names[1:-1])

    meta = events[0][1]
    assert set(meta) == {"turn_id", "trace_id", "conversation_id"}
    # 非 UUID 的客户端会话标识由服务端重新分配；meta 与 trace 用同一个值，保证可关联。
    assert meta["conversation_id"] == str(sink.turns[0].conversation_id)

    steps = [(payload["stage"], payload["label"]) for _, payload in events[1:-1]]
    assert [stage for stage, _ in steps] == ["route", "answer"]
    assert all(label for _, label in steps)

    done = events[-1][1]
    assert set(done) >= {
        "tokens_in",
        "tokens_out",
        "first_token_ms",
        "latency_ms",
        "stop_reason",
    }
    assert done["stop_reason"] == StopReason.FINAL_ANSWER.value
    assert done["trace_id"] == meta["trace_id"]
    latency_ms = done["latency_ms"]
    assert isinstance(latency_ms, int)
    assert latency_ms >= 0


@pytest.mark.asyncio
async def test_local_trace_is_mandatory_and_has_no_message_text(
    enabled_client: AsyncClient, sink: InMemoryTraceSink
) -> None:
    """本地 trace 必发；只记录元数据，不含用户消息原文。"""

    secret_text = "我的身份证号是123456789012345678"
    await enabled_client.post(ENDPOINT, json={"message": secret_text})

    assert len(sink.turns) == 1
    record = sink.turns[0]
    assert record.stop_reason == StopReason.FINAL_ANSWER
    assert record.error_code is None
    assert [span.name for span in record.spans] == ["route", "answer"]
    assert secret_text not in record.model_dump_json()


@pytest.mark.asyncio
async def test_client_abort_still_records_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    """客户端断开（生成器 aclose）时仍必须写入 trace，标记 CLIENT_CANCELLED。"""

    monkeypatch.setenv("DEV_SKELETON_LOOP_ENABLED", "true")
    get_settings.cache_clear()
    sink = InMemoryTraceSink()
    app = create_app(trace_sink=sink)

    response = await create_chat_turn(
        make_request(app), ChatTurnRequest(message="断开测试")
    )
    iterator = cast(AsyncGenerator[str], response.body_iterator)
    first_frame = await iterator.__anext__()
    assert "event: meta" in first_frame

    await iterator.aclose()  # 模拟客户端断线/取消

    assert len(sink.turns) == 1
    record = sink.turns[0]
    assert record.error_code == "CLIENT_CANCELLED"
    assert record.stop_reason is None


@pytest.mark.asyncio
async def test_empty_message_rejected_without_trace(
    enabled_client: AsyncClient, sink: InMemoryTraceSink
) -> None:
    """空消息 422，且不产生 trace。"""

    response = await enabled_client.post(ENDPOINT, json={"message": ""})

    assert response.status_code == 422
    assert sink.turns == []


@pytest.mark.asyncio
async def test_missing_trace_sink_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """trace sink 未注入时必须显式失败，不得把 trace 静默改道到临时 sink。"""

    monkeypatch.setenv("DEV_SKELETON_LOOP_ENABLED", "true")
    get_settings.cache_clear()
    app = create_app()
    app.state.trace_sink = None

    with pytest.raises(RuntimeError, match="trace sink 未注入"):
        await create_chat_turn(make_request(app), ChatTurnRequest(message="无 sink"))
