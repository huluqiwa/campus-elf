"""请求级租户/身份上下文。

上下文只由**认证层**（校验 JWT 之后）写入，绝不接受前端传入的 tenant_id/角色
（`docs/spec.md` §3.4）。仓储与会话注入均以本上下文为准。

生命周期：`begin_request()` → 认证层 `bind_context()`（可多次绑定/切换）→
请求结束由中间件 `clear_request_context()` 统一还原（防止协程复用泄漏）。
"""

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RequestContext:
    """一次请求的身份上下文。"""

    realm: str  # 'platform' | 'tenant'
    user_id: UUID
    tenant_id: UUID | None = None
    roles: frozenset[str] = field(default_factory=frozenset)
    permissions: frozenset[str] = field(default_factory=frozenset)
    data_scopes: tuple[str, ...] = ()
    request_id: str | None = None

    @property
    def is_platform(self) -> bool:
        """是否平台侧身份。"""

        return self.realm == "platform"


_context: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)
# 当前请求内所有已绑定上下文对应的还原令牌（后进先出还原）
_bound_tokens: ContextVar[list[Token[RequestContext | None]] | None] = ContextVar(
    "request_context_tokens", default=None
)


def begin_request() -> None:
    """开启一次请求的上下文作用域（中间件调用）。"""

    _bound_tokens.set([])


def bind_context(context: RequestContext) -> None:
    """绑定身份上下文（认证层调用）；同一请求内可重新绑定（如切换身份）。

    未先 `begin_request()`（如 Worker/CLI 场景）时会创建一个隐式作用域列表，
    保证 `clear_request_context()` 仍能还原，不会把身份泄漏给后续任务。
    """

    token = _context.set(context)
    tokens = _bound_tokens.get()
    if tokens is None:
        tokens = []
        _bound_tokens.set(tokens)
    tokens.append(token)


def clear_request_context() -> None:
    """还原本请求内所有绑定并清空（中间件在 finally 中调用）。"""

    tokens = _bound_tokens.get() or []
    for token in reversed(tokens):
        _context.reset(token)
    _bound_tokens.set(None)
    # 兜底：即使 Token 还原缺失（异常路径），也要保证上下文为空
    _context.set(None)


def current_context() -> RequestContext | None:
    """返回当前上下文；未认证时为 None。"""

    return _context.get()


def require_context() -> RequestContext:
    """返回当前上下文；未认证则报错。

    异常:
        PermissionError: 未认证（用于服务层强制鉴权的兜底）。
    """

    context = _context.get()
    if context is None:
        raise PermissionError("未认证：请求缺少身份上下文")
    return context
