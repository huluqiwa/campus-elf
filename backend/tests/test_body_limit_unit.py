"""请求体体积限制中间件单测（M3 KB-01A 流式模式）。

覆盖：
- 缓冲模式：`Content-Length` 预检、chunked 累计超限、未超限重放；
- 流式模式：不缓冲、chunked 超限 413 且抑制应用响应、恰好等于上限放行；
- 未登记路径完全透传（不加任何限制）。
"""

import asyncio
from typing import Any, cast

from starlette.types import Message, Receive, Scope, Send

from app.api.body_limit import BodyLimit, BodySizeLimitMiddleware


def _middleware(limits: dict[str, BodyLimit], received_by_app: list[bytes]) -> BodySizeLimitMiddleware:
    """构造中间件与「读完请求体后回 200」的假应用。"""

    async def fake_app(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            received_by_app.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        # 即便请求被判定超限，假应用仍尝试响应：用于验证流式模式会抑制应用响应
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return BodySizeLimitMiddleware(fake_app, limits=limits)


async def _call(
    middleware: BodySizeLimitMiddleware,
    *,
    chunks: list[bytes],
    path: str = "/upload",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> list[Message]:
    """驱动中间件一次；返回发送的响应消息序列。"""

    queue: asyncio.Queue[Message] = asyncio.Queue()
    for chunk in chunks:
        await queue.put({"type": "http.request", "body": chunk, "more_body": True})
    await queue.put({"type": "http.request", "body": b"", "more_body": False})

    async def receive() -> Message:
        return await queue.get()

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "path": path,
        "headers": headers or [],
        "method": "POST",
    }
    await middleware(scope, receive, send)
    return sent


def _status(messages: list[Message]) -> int | None:
    for message in messages:
        if message["type"] == "http.response.start":
            return int(message["status"])
    return None


async def test_buffered_mode_rejects_content_length_over_limit() -> None:
    received: list[bytes] = []
    middleware = _middleware({"/upload": BodyLimit(10, "太大")}, received)
    sent = await _call(middleware, chunks=[b"small"], headers=[(b"content-length", b"11")])
    assert _status(sent) == 413
    assert received == []


async def test_buffered_mode_rejects_chunked_over_limit() -> None:
    received: list[bytes] = []
    middleware = _middleware({"/upload": BodyLimit(10, "太大")}, received)
    sent = await _call(middleware, chunks=[b"12345", b"67890", b"x"])
    assert _status(sent) == 413


async def test_buffered_mode_replays_body_under_limit() -> None:
    received: list[bytes] = []
    middleware = _middleware({"/upload": BodyLimit(10, "太大")}, received)
    sent = await _call(middleware, chunks=[b"12345", b"6789"])
    assert _status(sent) == 200
    assert b"".join(received) == b"123456789"


async def test_streaming_mode_rejects_chunked_over_limit_and_suppresses_app_response() -> None:
    received: list[bytes] = []
    middleware = _middleware({"/upload": BodyLimit(10, "太大", buffer=False)}, received)
    sent = await _call(middleware, chunks=[b"12345", b"678901"])
    assert _status(sent) == 413
    assert len([m for m in sent if m["type"] == "http.response.start"]) == 1


async def test_streaming_mode_allows_body_at_limit() -> None:
    received: list[bytes] = []
    middleware = _middleware({"/upload": BodyLimit(10, "太大", buffer=False)}, received)
    sent = await _call(middleware, chunks=[b"12345", b"67890"])
    assert _status(sent) == 200
    assert b"".join(received) == b"1234567890"


async def test_unregistered_path_is_passthrough() -> None:
    received: list[bytes] = []
    middleware = _middleware({"/upload": BodyLimit(1, "太大")}, received)
    sent = await _call(middleware, chunks=[b"way over the limit"], path="/other")
    assert _status(sent) == 200
    assert b"".join(received) == b"way over the limit"


def _body_limits(app: Any) -> dict[str, BodyLimit]:
    """取出应用里注册的 BodyLimit 映射（starlette 的 kwargs 类型过宽，这里收窄）。"""

    for middleware in app.user_middleware:
        if cast(Any, middleware.cls) is BodySizeLimitMiddleware:
            return cast(dict[str, BodyLimit], middleware.kwargs["limits"])
    raise AssertionError("应用未注册 BodySizeLimitMiddleware")


def test_kb_upload_route_uses_streaming_limit() -> None:
    """KB 上传路径必须登记为流式 308MB 上限（不缓冲，chunked 也拦得住）。"""

    from app.config import get_settings
    from app.main import create_app

    kb_limit = _body_limits(create_app())[
        f"{get_settings().api_prefix}/school/kb/documents"
    ]
    assert kb_limit.buffer is False
    assert kb_limit.max_bytes == 308 * 1024 * 1024
