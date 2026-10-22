"""空壳 Loop SSE 测试（M0-F，M1-E1 起带鉴权主体）。

本文件只验证**接口内部行为**：开关关闭 404、事件字段与 `docs/design.md` §5.8 契约对齐、
「本地 trace 必发」（含中断路径）、trace 身份取自认证主体、不含消息原文。
因此直接调用路由函数并注入合成主体，不经过 HTTP 认证层。

HTTP 层的鉴权边界（401/403/跨 realm/权限热更新）见
`tests/integration/test_campus_chat_rbac.py`。
"""

import json
from collections.abc import AsyncGenerator, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.requests import Request as StarletteRequest

from app.api.campus_chat import ChatTurnRequest, create_chat_turn
from app.auth.principal import Principal
from app.config import get_settings
from app.harness.contracts import StopReason
from app.harness.trace import InMemoryTraceSink
from app.main import create_app

ENDPOINT = "/api/v1/campus/chat/turns"
CHAT_PERMISSION = "campus.chat.use"


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


def make_principal(*, permissions: tuple[str, ...] = (CHAT_PERMISSION,)) -> Principal:
    """合成一个租户主体（等价于 `require_permission` 校验通过后交给处理器的值）。"""

    now = datetime.now(UTC)
    return Principal(
        user_id=uuid4(),
        realm="tenant",
        tenant_id=uuid4(),
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=now + timedelta(minutes=15),
        must_change_password=False,
        roles=frozenset({"student"}),
        permissions=frozenset(permissions),
    )


async def drain(response: StreamingResponse) -> str:
    """消费流式响应体（等价于 HTTP 客户端读完整个 SSE 响应）。"""

    chunks = [str(chunk) async for chunk in cast(AsyncGenerator[str], response.body_iterator)]
    return "".join(chunks)


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
def enabled_app(monkeypatch: pytest.MonkeyPatch, sink: InMemoryTraceSink) -> Iterator[Any]:
    """开关打开的应用（开发/测试形态）。"""

    monkeypatch.setenv("DEV_SKELETON_LOOP_ENABLED", "true")
    get_settings.cache_clear()
    yield create_app(trace_sink=sink)
    # monkeypatch 只复位环境变量，`get_settings` 的 lru_cache 不会：不清就把开关泄露给后续用例。
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_disabled_returns_404_without_trace(sink: InMemoryTraceSink) -> None:
    """开关关闭时返回 404，且不产生 trace。"""

    app = create_app(trace_sink=sink)
    with pytest.raises(HTTPException) as excinfo:
        await create_chat_turn(make_request(app), ChatTurnRequest(message="你好"), make_principal())

    assert excinfo.value.status_code == 404
    assert sink.turns == []


@pytest.mark.asyncio
async def test_event_contract_and_stop_reason(enabled_app: Any, sink: InMemoryTraceSink) -> None:
    """事件序列为 meta → step* → done；字段与 §5.8 契约一致；响应头关闭缓冲。"""

    response = await create_chat_turn(
        make_request(enabled_app),
        ChatTurnRequest(message="图书馆几点开门", conversation_id="conv-1"),
        make_principal(),
    )

    assert response.media_type == "text/event-stream"
    assert response.headers["x-accel-buffering"] == "no"

    events = parse_sse(await drain(response))
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
async def test_valid_conversation_id_is_echoed(enabled_app: Any, sink: InMemoryTraceSink) -> None:
    """合法 UUID 会话标识沿用，保证客户端可续会话。"""

    conversation_id = uuid4()
    response = await create_chat_turn(
        make_request(enabled_app),
        ChatTurnRequest(message="食堂几点开门", conversation_id=str(conversation_id)),
        make_principal(),
    )

    events = parse_sse(await drain(response))
    assert events[0][1]["conversation_id"] == str(conversation_id)
    assert sink.turns[0].conversation_id == conversation_id


@pytest.mark.asyncio
async def test_local_trace_is_mandatory_and_has_no_message_text(
    enabled_app: Any, sink: InMemoryTraceSink
) -> None:
    """本地 trace 必发；只记录元数据，不含用户消息原文。"""

    secret_text = "我的身份证号是123456789012345678"
    response = await create_chat_turn(
        make_request(enabled_app), ChatTurnRequest(message=secret_text), make_principal()
    )
    await drain(response)

    assert len(sink.turns) == 1
    record = sink.turns[0]
    assert record.stop_reason == StopReason.FINAL_ANSWER
    assert record.error_code is None
    assert [span.name for span in record.spans] == ["route", "answer"]
    assert secret_text not in record.model_dump_json()


@pytest.mark.asyncio
async def test_trace_identity_comes_from_authenticated_principal(
    enabled_app: Any, sink: InMemoryTraceSink
) -> None:
    """M1-E1：trace 的租户/用户身份取认证主体，占位身份已删除。"""

    principal = make_principal()
    response = await create_chat_turn(
        make_request(enabled_app), ChatTurnRequest(message="占位身份检查"), principal
    )
    await drain(response)

    record = sink.turns[0]
    assert record.tenant_id == principal.tenant_id
    assert record.user_id == principal.user_id
    assert all(span.tenant_id == principal.tenant_id for span in record.spans)


@pytest.mark.asyncio
async def test_missing_tenant_context_is_rejected_without_trace(
    enabled_app: Any, sink: InMemoryTraceSink
) -> None:
    """租户主体缺少 tenant_id 时 403（不写 trace，也不产出跨租身份）。"""

    principal = replace(make_principal(), tenant_id=None)
    with pytest.raises(HTTPException) as excinfo:
        await create_chat_turn(
            make_request(enabled_app), ChatTurnRequest(message="缺少租户"), principal
        )

    assert excinfo.value.status_code == 403
    assert sink.turns == []


@pytest.mark.asyncio
async def test_client_abort_still_records_trace(enabled_app: Any, sink: InMemoryTraceSink) -> None:
    """客户端断开（生成器 aclose）时仍必须写入 trace，标记 CLIENT_CANCELLED。"""

    response = await create_chat_turn(
        make_request(enabled_app), ChatTurnRequest(message="断开测试"), make_principal()
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
async def test_empty_message_is_rejected_by_request_model() -> None:
    """空消息在请求模型层即被拒（HTTP 层表现为 422，见鉴权集成用例）。"""

    with pytest.raises(ValidationError):
        ChatTurnRequest(message="")


@pytest.mark.asyncio
async def test_missing_trace_sink_fails_loudly(enabled_app: Any) -> None:
    """trace sink 未注入时必须显式失败，不得把 trace 静默改道到临时 sink。"""

    enabled_app.state.trace_sink = None

    with pytest.raises(RuntimeError, match="trace sink 未注入"):
        await create_chat_turn(
            make_request(enabled_app), ChatTurnRequest(message="无 sink"), make_principal()
        )
