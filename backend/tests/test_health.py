"""健康检查测试（含依赖就绪判定）。"""

from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.readiness import DependencyCheck, collect_readiness
from app.main import app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """创建 ASGI 测试客户端。"""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _clear_dependency_overrides() -> Iterator[None]:
    """用例结束后清理依赖覆盖，避免相互影响。"""

    yield
    app.dependency_overrides.clear()


def _stub_checks(*checks: DependencyCheck) -> None:
    """用固定探活结果覆盖真实依赖检查。"""

    app.dependency_overrides[collect_readiness] = lambda: list(checks)


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
async def test_ready_all_ok(client: AsyncClient) -> None:
    """全部依赖探活通过时就绪为 ok。"""

    _stub_checks(
        DependencyCheck("postgres", "ok"),
        DependencyCheck("redis", "ok"),
        DependencyCheck("qdrant", "ok"),
    )

    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body["checks"]) == {"postgres", "redis", "qdrant"}
    assert all(item["status"] == "ok" for item in body["checks"].values())


@pytest.mark.asyncio
async def test_ready_not_configured_is_ok(client: AsyncClient) -> None:
    """未配置的依赖（开发机常见）不应导致不就绪。"""

    _stub_checks(
        DependencyCheck("postgres", "not_configured"),
        DependencyCheck("redis", "ok"),
        DependencyCheck("qdrant", "not_configured"),
    )

    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_ready_degrades_to_503_on_error(client: AsyncClient) -> None:
    """任一依赖探活失败即 503，并给出可读原因（不含凭证）。"""

    _stub_checks(
        DependencyCheck("postgres", "ok"),
        DependencyCheck("redis", "error", "ConnectionError"),
        DependencyCheck("qdrant", "ok"),
    )

    response = await client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["redis"] == {"status": "error", "detail": "ConnectionError"}


@pytest.mark.asyncio
async def test_health_not_under_api_prefix(client: AsyncClient) -> None:
    """健康检查不应挂在 /api/v1 下。"""

    response = await client.get("/api/v1/health/live")
    assert response.status_code == 404
