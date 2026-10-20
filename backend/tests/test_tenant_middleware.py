"""请求上下文中间件测试（接线与清理语义）。"""

import re
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.tenant import context as tenant_context
from app.tenant.context import RequestContext
from app.tenant.middleware import REQUEST_ID_HEADER


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """使用 create_app() 装配的应用（含中间件）。"""

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_request_id_is_echoed_when_provided(client: AsyncClient) -> None:
    """客户端提供 X-Request-ID 时原样回写（便于与前端/网关关联排障）。"""

    response = await client.get("/health/live", headers={REQUEST_ID_HEADER: "trace-abc-123"})

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == "trace-abc-123"


@pytest.mark.asyncio
async def test_request_id_is_generated_when_absent(client: AsyncClient) -> None:
    """未提供时由服务端生成 32 位十六进制 id。"""

    response = await client.get("/health/live")

    assert re.fullmatch(r"[0-9a-f]{32}", response.headers[REQUEST_ID_HEADER])


@pytest.mark.asyncio
async def test_context_is_cleared_after_request() -> None:
    """请求内绑定的身份上下文在请求结束后必须被清空（防协程复用泄漏）。"""

    app = create_app()

    @app.get("/__test/bind-context")
    async def bind_context_route() -> dict[str, bool]:
        tenant_context.bind_context(
            RequestContext(realm="tenant", user_id=uuid4(), tenant_id=uuid4())
        )
        return {"bound": tenant_context.current_context() is not None}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/__test/bind-context")

    assert response.json() == {"bound": True}
    assert tenant_context.current_context() is None, "请求结束后上下文未清理"
