"""M1-E2 集成测试：策略开关接口（决策 ⑧）。

钉住"入库而不是 env"这条口径成立的前提：

- 读要权限（`platform.config.view`），写要权限 + **必填理由**（`platform.config.edit`）；
- 改动与审计写在同一事务里：库里那行必须能回答"谁改的、为什么改"；
- 键白名单在代码里：库里/请求里出现未声明的键一律拒绝，不静默造一个新开关；
- 租户主体不得借道平台接口（realm 先于权限判定）。
"""

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.auth import revocation
from app.config import get_settings
from app.db.session import dispose_engine
from app.main import create_app
from app.settings.store import SELF_EXCEPTION_KEY, SETTING_DEFS
from tests.support.auth import login_platform, seed_platform_user
from tests.support.db import (
    clean_test_data,
    database_url,
    make_session_factory,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn

pytestmark = pytest.mark.integration

VIEWER = "e2-settings-viewer"
EDITOR = "e2-settings-editor"
NO_PERMISSION = "e2-settings-none"

CONFIG_VIEW = "platform.config.view"
CONFIG_EDIT = "platform.config.edit"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    """确保测试库结构最新（并做一次性测试库护栏）。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def env(migrated_database: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """清空业务数据、种三个平台账号，并把应用指向测试栈。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
        await seed_platform_user(admin_engine, account=VIEWER, permissions=(CONFIG_VIEW,))
        await seed_platform_user(
            admin_engine, account=EDITOR, permissions=(CONFIG_VIEW, CONFIG_EDIT)
        )
        await seed_platform_user(admin_engine, account=NO_PERMISSION, permissions=())
    finally:
        await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    get_settings.cache_clear()
    await revocation.reset_client()

    yield

    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()


@pytest.fixture
async def client(env: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _rows(sql: str, **params: object) -> list[tuple[object, ...]]:
    """管理连接上执行一条只读 SQL（测试内取数助手，不经过应用会话）。"""

    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            return [tuple(row) for row in (await session.execute(text(sql), params)).all()]
    finally:
        await engine.dispose()


async def _scalar(sql: str, **params: object) -> object:
    rows = await _rows(sql, **params)
    return rows[0][0] if rows else None


@pytest.mark.asyncio
async def test_list_settings_returns_declared_keys_with_defaults(
    client: AsyncClient,
) -> None:
    """库里还没有行时：接口回出厂默认，并明确标注"这不是库里的人工设置"。"""

    headers = await login_platform(client, VIEWER)
    response = await client.get("/api/v1/platform/settings", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert {item["key"] for item in body} == set(SETTING_DEFS)
    setting = next(item for item in body if item["key"] == SELF_EXCEPTION_KEY)
    assert setting["value"] is False
    assert setting["is_library_value"] is False
    assert setting["default_value"] is False
    assert setting["description"]


@pytest.mark.asyncio
async def test_put_setting_persists_reason_and_audits(client: AsyncClient) -> None:
    """改值：库里那行带理由与改动人，审计里能读出"谁、改成什么、为什么"。"""

    headers = await login_platform(client, EDITOR)
    response = await client.put(
        f"/api/v1/platform/settings/{SELF_EXCEPTION_KEY}",
        json={"value": True, "reason": "双人审核人同期休假，临时自批（工单 AUD-12）"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["value"] is True
    assert body["is_library_value"] is True

    stored = await _scalar(
        "select value || '|' || reason from public.system_settings where key = :key",
        key=SELF_EXCEPTION_KEY,
    )
    assert str(stored).startswith("true|")
    assert "AUD-12" in str(stored)

    audits = await _rows(
        "select resource_id, actor_realm, metadata_json from audit_logs "
        "where action = 'platform.setting.update' order by sequence desc limit 1"
    )
    assert len(audits) == 1, audits
    resource_id, actor_realm, metadata = audits[0]
    assert str(resource_id) == SELF_EXCEPTION_KEY
    assert actor_realm == "platform"
    assert isinstance(metadata, dict), metadata
    assert metadata["value"] is True  # 改后的值也要能从审计里读回
    assert "AUD-12" in metadata["reason"]


@pytest.mark.asyncio
async def test_put_setting_requires_reason(client: AsyncClient) -> None:
    """没有理由（空串/缺失）→ 422，且库里不能留下改动痕迹。"""

    headers = await login_platform(client, EDITOR)
    blank = await client.put(
        f"/api/v1/platform/settings/{SELF_EXCEPTION_KEY}",
        json={"value": True, "reason": "   "},
        headers=headers,
    )
    missing = await client.put(
        f"/api/v1/platform/settings/{SELF_EXCEPTION_KEY}",
        json={"value": True},
        headers=headers,
    )

    assert blank.status_code == 422, blank.text
    assert missing.status_code == 422, missing.text
    assert await _scalar("select count(*) from public.system_settings") == 0
    assert (
        await _scalar(
            "select count(*) from audit_logs where action = 'platform.setting.update'"
        )
        == 0
    )


@pytest.mark.asyncio
async def test_put_setting_rejects_unknown_key(client: AsyncClient) -> None:
    """未声明的键 → 404：接口不能成为"随手造一个没人审的开关"的入口。"""

    headers = await login_platform(client, EDITOR)
    response = await client.put(
        "/api/v1/platform/settings/approval.some_future_switch",
        json={"value": True, "reason": "想试试"},
        headers=headers,
    )

    assert response.status_code == 404, response.text
    assert await _scalar("select count(*) from public.system_settings") == 0


@pytest.mark.asyncio
async def test_settings_require_the_right_platform_permission(
    client: AsyncClient,
) -> None:
    """只读账号不能改值；无权限账号连读也不行（配置面同样按权限码收口）。"""

    viewer = await login_platform(client, VIEWER)
    write = await client.put(
        f"/api/v1/platform/settings/{SELF_EXCEPTION_KEY}",
        json={"value": True, "reason": "越权尝试"},
        headers=viewer,
    )
    none = await login_platform(client, NO_PERMISSION)
    read = await client.get("/api/v1/platform/settings", headers=none)

    assert write.status_code == 403, write.text
    assert read.status_code == 403, read.text
    assert await _scalar("select count(*) from public.system_settings") == 0
