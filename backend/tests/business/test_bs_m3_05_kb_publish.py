"""业务场景 BS-M3-05：知识库发布/热更新（M3 KB-05）——真实 PG/Redis/Qdrant。

Given 真实基础设施（PG/Redis/Qdrant；解析/嵌入 fake ports——真实引擎证据在 BS-M3-03，
TD-295 口径）；
When HTTP 全链：上传（dev-skip 扫描）→ 提交审批 → 审批通过 → 创建构建 →
     worker 管线 READY → 测试通过（脚手架置态，沙箱判分链路由 KB-04A 覆盖）→
     发布 / 回滚 / 下线 / 删除（HTTP）；
Then（对照 spec §KB-05 验收 :288-292）：
- 发布后 alias 行与 Qdrant named alias 指向新 collection，`active_publish`（M4 检索的
  数据基础）解析到新版本；
- 发布/回滚窗口内 200 并发解析：零异常、每次结果 ∈ {旧版本, 新版本} 且版本-collection
  恒配对（「引用版本正确、零 5xx」的 M3 可验证面——HTTP 检索面在 M4，本点钉住其
  数据基础的不变式）；
- 回滚后 `active_publish` 解析到回滚目标（新会话用回滚后版本）；
- 下线/删除后 collection 与向量保留（旧会话按版本直查仍可召回的数据层）、
  新会话解析 fail-closed 返回 None；跨租 404 / None，B 租发布自己的文档可达。
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.config import get_settings
from app.kb.build_task import build_kb_version
from app.kb.parser import ParsedDocument
from app.kb.service import active_publish
from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import (
    B_BUILDER,
    B_UPLOADER,
    KB_APPROVER,
    KB_B_PUBLISHER,
    KB_BUILDER,
    SCHOOL_B,
    UPLOADER,
    KbEnv,
    execute,
    kb_env,
    login,
    make_client,
    rows,
    upload,
)
from tests.support.kb_publish_seed import (
    MARKDOWN,
    FakeParser,
    patch_ports,
    publisher_headers,
    seed_test_passed_version,
    seed_two_published_versions,
)

pytestmark = [pytest.mark.business, pytest.mark.integration]


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


async def _chain_to_test_passed(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> tuple[UUID, UUID]:
    """HTTP 全链到 TEST_PASSED：上传 → 提交 → 通过 → 创建构建 → worker READY → 置态。"""

    patch_ports(monkeypatch, FakeParser(ParsedDocument(markdown=MARKDOWN)))
    uploader_headers = await login(client, UPLOADER)
    uploaded = await upload(
        client,
        uploader_headers,
        key=f"bs05-{uuid4().hex[:16]}",
        files=[("bs05.md", MARKDOWN.encode("utf-8"), "text/markdown")],
        metadata='[{"title": "发布业务场景文档", "tags": ["手册"]}]',
    )
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()["documents"][0]
    assert document["status"] == "PENDING_APPROVAL"
    doc_id = UUID(document["id"])
    version_id = UUID(document["current_version"]["id"])

    submitted = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/submit-approval",
        headers=uploader_headers,
    )
    assert submitted.status_code == 201, submitted.text
    request_id = submitted.json()["request_id"]

    approver_headers = await login(client, KB_APPROVER)
    approved = await client.post(
        f"/api/v1/school/approvals/{request_id}/approve",
        headers=approver_headers,
        json={},
    )
    assert approved.status_code == 200, approved.text

    # K4 门禁接线（卡 4）：构建前配 all+确认
    from tests.support.kb_audience_seed import configure_audiences

    await configure_audiences(env.tenant_a, version_id)
    built = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/build",
        headers=await login(client, KB_BUILDER),
    )
    assert built.status_code == 201, built.text

    result = await build_kb_version({}, str(env.tenant_a), str(version_id))
    assert result["status"] == "ready", result
    await execute(
        "update kb_document_versions set status = 'TEST_PASSED' where id = :id",
        id=str(version_id),
    )
    await execute(
        "update kb_documents set status = 'TEST_PASSED' "
        "where id = :doc and current_version_id = :version",
        doc=str(doc_id),
        version=str(version_id),
    )
    return doc_id, version_id


# ---------------------------------------------------------------------------
# 全链 + 热更新语义
# ---------------------------------------------------------------------------


async def test_full_chain_publish_rollback_and_active_resolution(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """全链发布 + 版本间切换：active_publish 始终解析到活跃发布（回滚后=回滚目标）。"""

    from app.kb.indexer import alias_name, collection_name

    doc_id, v1 = await _chain_to_test_passed(env, monkeypatch, client)
    headers = await publisher_headers(client)

    # 发布 v1
    published = await client.post(f"/api/v1/school/kb/versions/{v1}/publish", headers=headers)
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["collection"] == collection_name(env.tenant_a, v1)
    resolved = await active_publish(tenant_id=env.tenant_a, document_id=doc_id)
    assert resolved is not None and resolved[0] == v1

    # 同文档第二版本（内容修改 → 新版本；构建走真实管线 + fake ports）
    _, v2 = await seed_test_passed_version(
        env, monkeypatch, doc_id=doc_id, version_no=2, title="发布业务场景文档"
    )
    published_v2 = await client.post(
        f"/api/v1/school/kb/versions/{v2}/publish", headers=headers
    )
    assert published_v2.status_code == 200, published_v2.text
    assert published_v2.json()["previous_version_id"] == str(v1)
    resolved = await active_publish(tenant_id=env.tenant_a, document_id=doc_id)
    assert resolved is not None and resolved[0] == v2
    assert resolved[1] == collection_name(env.tenant_a, v2)

    # 回滚到 v1：新会话解析到回滚目标
    rolled = await client.post(f"/api/v1/school/kb/versions/{v1}/rollback", headers=headers)
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["alias"] == alias_name(env.tenant_a, doc_id)
    resolved = await active_publish(tenant_id=env.tenant_a, document_id=doc_id)
    assert resolved is not None and resolved[0] == v1

    # 两版本并存 PUBLISHED（蓝绿），可再切回 v2
    again = await client.post(f"/api/v1/school/kb/versions/{v2}/rollback", headers=headers)
    assert again.status_code == 200, again.text
    resolved = await active_publish(tenant_id=env.tenant_a, document_id=doc_id)
    assert resolved is not None and resolved[0] == v2

    # Qdrant named alias 跟随指针行
    from qdrant_client import AsyncQdrantClient

    settings = get_settings()
    qclient = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        aliases = await qclient.get_aliases()
        matched = [a for a in aliases.aliases if a.alias_name == alias_name(env.tenant_a, doc_id)]
        assert len(matched) == 1
        assert matched[0].collection_name == collection_name(env.tenant_a, v2)
    finally:
        await qclient.close()


async def test_200_concurrent_reads_during_rollback_window(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """发布/回滚窗口并发解析：零异常、结果恒 ∈ {v1, v2} 且版本-collection 配对。

    TD-309 加固（2026-11-11 用户拍板本 PR 顺带修；原实现 20 次切换与 1000 次
    读取同时起跑，「两种解析结果都出现过」依赖调度时序，CI 负载下 2/2 flake）：
    先各完成一次真实切换 + 一次读取（v1/v2 各得一次**确定性**观察），其余
    18 次切换与 1000 次读取并发跑窗口——核心不变量断言（配对/非空/零异常/
    两种版本都被观察到）一条不降，只把时序依赖换成确定性交错。
    """

    from app.kb.indexer import collection_name

    doc_id, v1, v2 = await seed_two_published_versions(env, monkeypatch, client)
    headers = await publisher_headers(client)
    pairs = {
        (str(v1), collection_name(env.tenant_a, v1)),
        (str(v2), collection_name(env.tenant_a, v2)),
    }

    # 确定性前奏：v1、v2 各完成一次真实切换 + 各一次读取（串行，无竞态）
    manual_reads: list[tuple[str, str]] = []
    for target in (v1, v2):
        response = await client.post(
            f"/api/v1/school/kb/versions/{target}/rollback", headers=headers
        )
        assert response.status_code == 200, response.text
        resolved = await active_publish(tenant_id=env.tenant_a, document_id=doc_id)
        assert resolved is not None, "切换窗口内解析不得为空（fail-closed 只对未发布态）"
        manual_reads.append((str(resolved[0]), resolved[1]))

    async def switcher() -> int:
        switched = 0
        for target in (v1, v2) * 9:
            response = await client.post(
                f"/api/v1/school/kb/versions/{target}/rollback", headers=headers
            )
            assert response.status_code == 200, response.text
            switched += 1
        return switched

    async def reader() -> list[tuple[str, str]]:
        observed: list[tuple[str, str]] = []
        for _ in range(5):
            resolved = await active_publish(tenant_id=env.tenant_a, document_id=doc_id)
            assert resolved is not None, "切换窗口内解析不得为空（fail-closed 只对未发布态）"
            observed.append((str(resolved[0]), resolved[1]))
        return observed

    switch_task = asyncio.create_task(switcher())
    reader_tasks = [asyncio.create_task(reader()) for _ in range(200)]
    switched_rest = await switch_task
    reads = manual_reads + [
        item for sub in await asyncio.gather(*reader_tasks) for item in sub
    ]

    assert manual_reads == [
        (str(v1), collection_name(env.tenant_a, v1)),
        (str(v2), collection_name(env.tenant_a, v2)),
    ]
    assert switched_rest + 2 == 20
    assert len(reads) == 1002
    assert all(pair in pairs for pair in reads)
    # 切换真实发生了：两种解析结果都出现过（前奏确定性保证；并发段只增配对合法行）
    assert {version for version, _ in reads} == {str(v1), str(v2)}


async def test_offline_delete_preserve_collection_and_fail_closed(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """下线/删除：collection 保留（旧会话按版本直查仍可召回）；解析 fail-closed。"""

    from qdrant_client import AsyncQdrantClient

    from app.kb.indexer import collection_name, count_points

    doc_id, version_id = await _chain_to_test_passed(env, monkeypatch, client)
    headers = await publisher_headers(client)
    published = await client.post(
        f"/api/v1/school/kb/versions/{version_id}/publish", headers=headers
    )
    assert published.status_code == 200, published.text

    collection = collection_name(env.tenant_a, version_id)
    offline = await client.post(
        f"/api/v1/school/kb/versions/{version_id}/offline", headers=headers
    )
    assert offline.status_code == 200, offline.text

    # 旧会话数据层：collection 与点仍在（按版本直查可召回）
    settings = get_settings()
    qclient = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        assert await qclient.collection_exists(collection)
        assert await count_points(qclient, collection=collection) > 0
    finally:
        await qclient.close()
    assert await active_publish(tenant_id=env.tenant_a, document_id=doc_id) is None

    # 删除路径：另起一个文档发布后软删
    doc2, version2 = await _chain_to_test_passed(env, monkeypatch, client)
    ok = await client.post(f"/api/v1/school/kb/versions/{version2}/publish", headers=headers)
    assert ok.status_code == 200, ok.text
    deleted = await client.delete(f"/api/v1/school/kb/versions/{version2}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    collection2 = collection_name(env.tenant_a, version2)
    qclient = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        assert await qclient.collection_exists(collection2)
        assert await count_points(qclient, collection=collection2) > 0
    finally:
        await qclient.close()
    assert await active_publish(tenant_id=env.tenant_a, document_id=doc2) is None


async def test_cross_tenant_isolation(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """跨租：B 租发布人对 A 租版本 404；B 租解析 A 租文档为 None；A 租不受影响。"""

    doc_id, version_id = await _chain_to_test_passed(env, monkeypatch, client)

    b_headers = await login(client, KB_B_PUBLISHER, SCHOOL_B)
    for method, path in (
        ("post", "publish"),
        ("post", "rollback"),
        ("post", "offline"),
    ):
        response = await getattr(client, method)(
            f"/api/v1/school/kb/versions/{version_id}/{path}", headers=b_headers
        )
        assert response.status_code == 404, (method, path, response.text)
    assert (
        await client.delete(f"/api/v1/school/kb/versions/{version_id}", headers=b_headers)
    ).status_code == 404

    assert await active_publish(tenant_id=env.tenant_b, document_id=doc_id) is None

    # A 租发布不受影响（未发布态 → 发布可达）
    headers = await publisher_headers(client)
    ok = await client.post(f"/api/v1/school/kb/versions/{version_id}/publish", headers=headers)
    assert ok.status_code == 200, ok.text


async def test_b_tenant_publisher_publishes_own_tenant(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """B 租发布人发布 B 租文档可达（同款管线；租户边界互不影响）。"""

    from app.kb.indexer import collection_name

    doc_id, version_id = await _seed_b_tenant_version(env, monkeypatch)
    headers = await publisher_headers(client, login_id=KB_B_PUBLISHER, school_code=SCHOOL_B)
    published = await client.post(
        f"/api/v1/school/kb/versions/{version_id}/publish", headers=headers
    )
    assert published.status_code == 200, published.text
    assert published.json()["collection"] == collection_name(env.tenant_b, version_id)
    resolved = await active_publish(tenant_id=env.tenant_b, document_id=doc_id)
    assert resolved is not None and resolved[0] == version_id


async def _seed_b_tenant_version(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> tuple[UUID, UUID]:
    """B 租 TEST_PASSED 版本（与 A 租脚手架同款 SQL，换 tenant_b；构建走 fake ports）。"""

    from app.storage.objects import build_object_store

    patch_ports(monkeypatch, FakeParser(ParsedDocument(markdown=MARKDOWN)))
    doc_id, version_id = uuid4(), uuid4()
    object_key = f"kb/{env.tenant_b}/{doc_id}/{version_id}/source.md"
    store = build_object_store(get_settings())
    await store.put(object_key, MARKDOWN.encode("utf-8"), "text/markdown")
    creator = await rows(
        "select u.id from users u join tenants t on t.id = u.tenant_id "
        "where u.login_id = :lid and t.school_code = :code",
        lid=B_BUILDER,
        code=SCHOOL_B,
    )
    creator_id = str(creator[0]["id"])
    batch_id = uuid4()
    await execute(
        """
        insert into kb_upload_batches (id, tenant_id, idempotency_key, request_hash,
                                       file_count, total_bytes, created_by)
        values (:id, :tenant, :key, :hash, 1, 100, :creator)
        """,
        id=str(batch_id),
        tenant=str(env.tenant_b),
        key=f"bs05b-{uuid4().hex[:12]}",
        hash="e" * 64,
        creator=creator_id,
    )
    await execute(
        """
        insert into kb_documents (id, tenant_id, title, tags, status,
                                  upload_batch_id, upload_file_index, created_by)
        values (:doc, :tenant, 'B 租发布文档', '[]'::jsonb, 'APPROVED', :batch, 0, :creator)
        """,
        doc=str(doc_id),
        tenant=str(env.tenant_b),
        batch=str(batch_id),
        creator=creator_id,
    )
    await execute(
        """
        insert into kb_document_versions (id, tenant_id, document_id, version_no,
                                          source_object_key, source_filename, source_hash,
                                          mime, size_bytes, status, scan_engine, scanned_at,
                                          created_by)
        values (:id, :tenant, :doc, 1, :key, 'b.md', :hash, 'text/markdown', 100,
                'APPROVED', 'dev-skip', now(), :creator)
        """,
        id=str(version_id),
        tenant=str(env.tenant_b),
        doc=str(doc_id),
        key=object_key,
        hash="f" * 64,
        creator=creator_id,
    )
    await execute(
        "update kb_documents set current_version_id = :version where id = :doc",
        version=str(version_id),
        doc=str(doc_id),
    )
    await execute(
        """
        insert into kb_build_jobs (id, tenant_id, version_id, status, attempt, retry_count, created_by)
        values (:id, :tenant, :version, 'BUILD_QUEUED', 1, 0, :creator)
        """,
        id=str(uuid4()),
        tenant=str(env.tenant_b),
        version=str(version_id),
        creator=creator_id,
    )
    from tests.support.kb_audience_seed import configure_audiences

    await configure_audiences(env.tenant_b, version_id, by_login_id=B_UPLOADER)
    result = await build_kb_version({}, str(env.tenant_b), str(version_id))
    assert result["status"] == "ready", result
    await execute(
        "update kb_document_versions set status = 'TEST_PASSED' where id = :id",
        id=str(version_id),
    )
    await execute(
        "update kb_documents set status = 'TEST_PASSED' "
        "where id = :doc and current_version_id = :version",
        doc=str(doc_id),
        version=str(version_id),
    )
    return doc_id, version_id
