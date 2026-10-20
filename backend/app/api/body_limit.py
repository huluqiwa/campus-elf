"""请求体大小限制（按路由，M1-B）。

只有明确登记的路由才在进入应用前缓冲并校验体积（注册接口 11MB）：
- 有 `Content-Length` 时直接拒绝超限请求；
- **chunked/无 Content-Length** 时先缓冲并累计字节数，超限返回 413，
  避免 multipart 解析把大文件写入磁盘/内存；未超限时把缓冲体重放给应用。
未登记路由**不做缓冲**（原始 `receive` 直接透传），避免给后续知识库 50MB/300MB
上传引入全局上限与内存放大。代理层 `client_max_body_size` 是第二道防线。
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodySizeLimitMiddleware:
    """对登记路径限制请求体大小（超限 413，含 chunked）。"""

    def __init__(self, app: ASGIApp, *, limits: dict[str, int]) -> None:
        """初始化中间件。

        参数:
            limits: 路径 → 最大字节数（按 `scope["path"]` 精确匹配）。
        """

        self.app = app
        self.limits = limits

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """仅对登记路径执行体积校验，其余请求原样透传。"""

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "").rstrip("/") or "/"
        max_bytes = self.limits.get(path)
        if max_bytes is None:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                length = int(raw_length)
            except ValueError:
                length = 0
            if length > max_bytes:
                await _send_413(send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > max_bytes:
                # 不继续读取剩余 body：由 ASGI server 关闭/丢弃，应用不会被解析
                await _send_413(send)
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


async def _send_413(send: Send) -> None:
    """发送 JSON 413（不经过异常处理器，保持中间件无依赖）。"""

    import json

    payload = json.dumps(
        {"detail": "请求体过大（资质凭证不超过 10MB）"}, ensure_ascii=False
    ).encode()
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
