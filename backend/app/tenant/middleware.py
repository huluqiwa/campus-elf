"""租户上下文中间件（纯 ASGI 实现）。

为什么不用 `BaseHTTPMiddleware`：它会把下游应用放到**另一个 Context** 里执行，
`ContextVar` 的 Token 无法在父 Context 中还原（实测 `ValueError: Token was created in a
different Context`）。纯 ASGI 中间件在同一 Context 内运行，上下文的绑定与还原语义才可靠。

职责边界：
- 生成/校验 `request_id`（回写响应头，便于排障与审计关联）；
- 请求结束时**清空**身份上下文（防止协程复用造成上下文泄漏）；
- 真正的身份注入在 M1-C 的认证依赖里完成（校验 JWT 后 `bind_context`）——
  中间件**不读取**任何请求头/请求体中的 tenant_id、角色信息（不可信前端输入）。
"""

import re
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.tenant import context as tenant_context

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _resolve_request_id(scope: Scope) -> str:
    """取客户端 request_id（合法才采用），否则生成 32 位十六进制 id。

    客户端值必须限制长度与字符集，否则会污染审计列（varchar(64)）与日志。
    """

    headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    for name, value in headers:
        if name.decode("latin-1").lower() == REQUEST_ID_HEADER.lower():
            candidate = value.decode("latin-1").strip()
            if _REQUEST_ID_PATTERN.fullmatch(candidate):
                return candidate
            break
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """纯 ASGI 中间件：request_id 注入 + 请求级上下文清理。"""

    def __init__(self, app: ASGIApp) -> None:
        """保存下游应用。"""

        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """处理一次 ASGI 调用。"""

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _resolve_request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id
        tenant_context.begin_request()

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # 认证依赖可能绑定了上下文；请求结束必须清空，避免泄漏到下一个请求
            tenant_context.clear_request_context()
