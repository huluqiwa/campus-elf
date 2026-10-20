"""请求上下文生命周期测试。"""

from uuid import uuid4

import pytest

from app.tenant import context as tenant_context
from app.tenant.context import RequestContext, require_context


def _tenant_context() -> RequestContext:
    return RequestContext(realm="tenant", user_id=uuid4(), tenant_id=uuid4(), roles=frozenset({"student"}))


def test_bind_and_clear_lifecycle() -> None:
    """请求作用域内绑定、结束后必须完全清空（防协程复用泄漏）。"""

    assert tenant_context.current_context() is None

    tenant_context.begin_request()
    tenant_context.bind_context(_tenant_context())
    assert tenant_context.current_context() is not None

    tenant_context.clear_request_context()
    assert tenant_context.current_context() is None


def test_rebind_within_request_and_clear_restores_none() -> None:
    """同请求内多次绑定（如身份切换）后统一还原为 None。"""

    tenant_context.begin_request()
    first = _tenant_context()
    second = _tenant_context()
    tenant_context.bind_context(first)
    tenant_context.bind_context(second)
    assert tenant_context.current_context() is second

    tenant_context.clear_request_context()
    assert tenant_context.current_context() is None


def test_require_context_raises_when_unauthenticated() -> None:
    """未认证时服务层兜底报错（而不是当成"无租户可访问全部"）。"""

    tenant_context.begin_request()
    try:
        with pytest.raises(PermissionError, match="未认证"):
            require_context()
    finally:
        tenant_context.clear_request_context()


def test_platform_flag() -> None:
    """平台身份标识。"""

    platform = RequestContext(realm="platform", user_id=uuid4())

    assert platform.is_platform is True
    assert _tenant_context().is_platform is False
