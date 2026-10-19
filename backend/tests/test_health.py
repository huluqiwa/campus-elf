"""健康检查测试。"""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """创建 ASGI 测试客户端。"""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_live(client: AsyncClient) -> None:
    """存活检查在根路径返回完整响应。"""

    response = await client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "CampusCompanionElf"
    assert body["version"] == "0.1.0"
    assert body["env"] == "development"


@pytest.mark.asyncio
async def test_ready(client: AsyncClient) -> None:
    """就绪检查在根路径返回完整响应。"""

    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "CampusCompanionElf"
    assert body["version"] == "0.1.0"
    assert body["env"] == "development"


@pytest.mark.asyncio
async def test_health_not_under_api_prefix(client: AsyncClient) -> None:
    """健康检查不应挂在 /api/v1 下。"""

    response = await client.get("/api/v1/health/live")
    assert response.status_code == 404
