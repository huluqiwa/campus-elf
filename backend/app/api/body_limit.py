"""请求体大小限制（按路由，M1-B；M3 增加流式模式）。

只有明确登记的路由才校验体积：
- 默认**缓冲模式**（注册/导入）：有 `Content-Length` 时直接拒绝超限请求；chunked/无
  `Content-Length` 时缓冲并累计，超限 413，未超限时把缓冲体重放给应用；
- **流式模式**（知识库 308MB）：不缓冲，逐块累计并原样透传；超限后丢弃应用响应并统一 413，
  避免把 300MB 读进内存。应用若已开始响应则无法改判（此时按已发送内容结束）。
未登记路由**不做任何处理**（原始 `receive` 直接透传）。代理层
`client_max_body_size` 是第二道防线。
"""

import json
from dataclasses import dataclass

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_DEFAULT_MESSAGE = "请求体过大"


@dataclass(frozen=True, slots=True)
class BodyLimit:
    """单条路由的体积上限。

    参数:
        max_bytes: 允许的最大请求体字节数。
        message: 413 响应里的中文说明（按业务区分，避免误导）。
        buffer: True 缓冲后重放；False 流式计数（大上传，不占内存）。
    """

    max_bytes: int
    message: str = _DEFAULT_MESSAGE
    buffer: bool = True


class BodySizeLimitMiddleware:
    """对登记路径限制请求体大小（超限 413，含 chunked）。"""

    def __init__(self, app: ASGIApp, *, limits: dict[str, BodyLimit]) -> None:
        """初始化中间件。

        参数:
            limits: 路径 → 体积策略（按 `scope["path"]` 精确匹配）。
        """

        self.app = app
        self.limits = limits

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """仅对登记路径执行体积校验，其余请求原样透传。"""

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "").rstrip("/") or "/"
        limit = self.limits.get(path)
        if limit is None:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                length = int(raw_length)
            except ValueError:
                length = 0
            if length > limit.max_bytes:
                await _send_413(send, limit.message)
                return

        if limit.buffer:
            await self._run_buffered(scope, receive, send, limit)
        else:
            await self._run_streaming(scope, receive, send, limit)

    async def _run_buffered(
        self, scope: Scope, receive: Receive, send: Send, limit: BodyLimit
    ) -> None:
        """缓冲模式：完整读入后重放（仅小上传）。"""

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > limit.max_bytes:
                # 不继续读取剩余 body：由 ASGI server 关闭/丢弃，应用不会被解析
                await _send_413(send, limit.message)
                return
            if not message.get("more_body", False):
                break

        replayed = False
        buffered = bytes(body)

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": buffered,
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _run_streaming(
        self, scope: Scope, receive: Receive, send: Send, limit: BodyLimit
    ) -> None:
        """流式模式：只计数不缓存；超限后抑制应用响应并统一 413。"""

        received = 0
        too_large = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit.max_bytes:
                    too_large = True
                    # 让应用停止读取：解析器会以 ClientDisconnect 结束，响应随后被抑制
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if too_large and not response_started:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not too_large:
                raise
        if too_large and not response_started:
            await _send_413(send, limit.message)


async def _send_413(send: Send, message: str) -> None:
    """发送 JSON 413（不经过异常处理器，保持中间件无依赖）。"""

    payload = json.dumps({"detail": message}, ensure_ascii=False).encode()
    start: Message = {
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(payload)).encode()),
        ],
    }
    await send(start)
    await send({"type": "http.response.body", "body": payload})
