"""业务场景 BS-M3-08：知识库版本切换 MVP（M3 KB-08）——真实 PG/Redis/Qdrant。

Given 真实基础设施（PG/Redis/Qdrant；解析/嵌入 fake ports——真实引擎证据在 BS-M3-03，
TD-295 口径）与同一文档两个 TEST_PASSED 版本（各自经真实构建管线 READY、内容互异），
HTTP 发布 v1 → v2；
When 校方发布人查看文档详情、回滚切回旧版本 v1（POST /versions/{v1}/rollback，
D2-A——版本切换即既有回滚端点，动作面零新增）、再查看详情与两版本分块列表
（school.kb.view_chunks）；
Then（对照 spec §KB-06/07/08 验收 :302-303）：
- 发布后详情 `active_version_id` = v2；回滚后详情 `active_version_id` = v1
  （「切换到旧版本」的服真化面，TD-305——kb_publish_aliases 库行是事实来源）；
- v1 分块列表只含 v1 内容（含 V1 独有标记、不含 V2 独有标记），v2 列表反之，
  两列表 chunk_id 交集为空（「展示该版本分块，不混入新版本」）；
- 跨租详情/分块一律 404 不泄露存在性（KB_B_PUBLISHER / KB_B_CHUNK_VIEWER）。
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from httpx import AsyncClient

from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import (
    KB_B_CHUNK_VIEWER,
    KB_B_PUBLISHER,
    KB_CHUNK_VIEWER,
    SCHOOL_B,
    KbEnv,
    kb_env,
    login,
    make_client,
)
from tests.support.kb_publish_seed import (
    publisher_headers,
    seed_test_passed_version,
)

pytestmark = [pytest.mark.business, pytest.mark.integration]

MARKDOWN_V1 = "# 版本切换手册\n\n第一版独有内容 V1-ONLY-MARKER。\n\n## 使用说明\n\n第一版公共说明段落。\n"
MARKDOWN_V2 = "# 版本切换手册\n\n第二版独有内容 V2-ONLY-MARKER。\n\n## 使用说明\n\n第二版公共说明段落。\n"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def env(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[KbEnv]:
    async with kb_env(
        monkeypatch,
        tmp_path,
        overrides={
            "QDRANT_URL": "http://127.0.0.1:56333",
            "DEV_SKIP_FILE_SCAN": "true",
        },
    ) as value:
        yield value


@pytest.fixture
async def client(env: KbEnv) -> AsyncIterator[AsyncClient]:
    async with make_client() as ac:
        yield ac


async def test_version_switch_shows_old_version_chunks_without_mixing(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """spec :302-303 全链：切换到旧版本 → 查看分块不混入新版本；跨租 404。"""

    # ---- Given：同一文档两版本（内容互异），真实构建 READY，HTTP 发布 v1 → v2 ----
    doc_id, v1 = await seed_test_passed_version(
        env, monkeypatch, version_no=1, title="版本切换文档", markdown=MARKDOWN_V1
    )
    _doc2, v2 = await seed_test_passed_version(
        env,
        monkeypatch,
        doc_id=doc_id,
        version_no=2,
        title="版本切换文档",
        markdown=MARKDOWN_V2,
    )
    headers = await publisher_headers(client)
    first = await client.post(f"/api/v1/school/kb/versions/{v1}/publish", headers=headers)
    assert first.status_code == 200, first.text
    second = await client.post(f"/api/v1/school/kb/versions/{v2}/publish", headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["previous_version_id"] == str(v1)

    detail_url = f"/api/v1/school/kb/documents/{doc_id}"

    # ---- 发布 v2 后：详情活跃指针 = v2（TD-305 服真化面） ----
    published_detail = (await client.get(detail_url, headers=headers)).json()
    assert published_detail["active_version_id"] == str(v2)

    # ---- When：切换回旧版本 v1（回滚端点即版本切换；当前活跃保持 PUBLISHED） ----
    rollback = await client.post(f"/api/v1/school/kb/versions/{v1}/rollback", headers=headers)
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["previous_version_id"] == str(v2)

    # ---- Then：详情活跃指针 = 旧版本 v1 ----
    switched_detail = (await client.get(detail_url, headers=headers)).json()
    assert switched_detail["active_version_id"] == str(v1)

    # ---- Then：查看分块——该版本分块，不混入新版本（spec :302） ----
    chunk_viewer = await login(client, KB_CHUNK_VIEWER)
    v1_page = await client.get(
        f"/api/v1/school/kb/versions/{v1}/chunks", headers=chunk_viewer
    )
    v2_page = await client.get(
        f"/api/v1/school/kb/versions/{v2}/chunks", headers=chunk_viewer
    )
    assert v1_page.status_code == 200, v1_page.text
    assert v2_page.status_code == 200, v2_page.text
    v1_items = v1_page.json()["items"]
    v2_items = v2_page.json()["items"]
    v1_ids = {item["id"] for item in v1_items}
    v2_ids = {item["id"] for item in v2_items}
    assert v1_ids, "v1 应有分块"
    assert v2_ids, "v2 应有分块"
    assert v1_ids & v2_ids == set(), "两版本分块行不得混入"
    v1_text = "".join(item["text"] for item in v1_items)
    v2_text = "".join(item["text"] for item in v2_items)
    assert "V1-ONLY-MARKER" in v1_text
    assert "V2-ONLY-MARKER" not in v1_text, "v1 分块不得混入 v2 内容"
    assert "V2-ONLY-MARKER" in v2_text
    assert "V1-ONLY-MARKER" not in v2_text, "v2 分块不得混入 v1 内容"

    # ---- Then：跨租详情/分块一律 404，不泄露存在性（spec :303） ----
    b_publisher = await login(client, KB_B_PUBLISHER, SCHOOL_B)
    cross_detail = await client.get(detail_url, headers=b_publisher)
    assert cross_detail.status_code == 404
    assert cross_detail.json()["detail"]["code"] == "DOCUMENT_NOT_FOUND"

    b_viewer = await login(client, KB_B_CHUNK_VIEWER, SCHOOL_B)
    cross_chunks = await client.get(
        f"/api/v1/school/kb/versions/{v1}/chunks", headers=b_viewer
    )
    assert cross_chunks.status_code == 404
    assert cross_chunks.json()["detail"]["code"] == "VERSION_NOT_FOUND"
