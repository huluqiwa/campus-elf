"""业务场景 BS-M3-09：文档查阅人群 ACL 门禁与发布链（M3 KB-09A，K4/TD-302）。

Given 真实基础设施（PG/Redis/Qdrant；解析/嵌入 fake ports——真实引擎证据在 BS-M3-03，
TD-295 口径）；
When HTTP 全链：上传（dev-skip 扫描）→ 提交审批 → 审批通过 →（人群门禁）
     未配置构建 409 → 配置未确认构建 409 → 确认 → 构建 READY → payload 检查 →
     发布 →（PUT 置未确认）再发布 409 → 重新确认 → 发布恢复；
Then（对照 spec §KB-09 验收 :315-321）：
- 未配置 ACL 构建/发布 409 `ACL_REQUIRED` 并审计（build.denied / publish.denied），
  零任务行/零指针行（fail-closed 副作用面）；
- 已配置未确认 409 `ACL_UNCONFIRMED`；确认后构建/发布可达；
- 构建完成后 Qdrant 每个 chunk 携带该版本 ACL token 列表（验收 :321）；
- PUT 置未确认 → payload 立即清空（检索恒不召回 fail-closed）→ 确认恢复新 tokens；
- 跨租 audiences 访问 404（B 租持 view 探测，不泄露存在性）。
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.kb.build_task import build_kb_version
from app.kb.parser import ParsedDocument
from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_audience_seed import configure_audiences
from tests.support.kb_env import (
    KB_ACL_MANAGER,
    KB_APPROVER,
    KB_B_PUBLISHER,
    KB_BUILDER,
    KB_PUBLISHER,
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
)

pytestmark = [pytest.mark.business, pytest.mark.integration]

AUDIENCES_URL = "/api/v1/school/kb/document-versions/{version_id}/audiences"
BUILD_URL = "/api/v1/school/kb/document-versions/{version_id}/build"
PUBLISH_URL = "/api/v1/school/kb/versions/{version_id}/publish"


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
    """真实 PG/Redis/Qdrant + 本地对象存储；解析/嵌入 fake ports。"""

    async with kb_env(
        monkeypatch, tmp_path, overrides={"QDRANT_URL": "http://127.0.0.1:56333"}
    ) as value:
        yield value


@pytest.fixture
async def client(env: KbEnv) -> AsyncIterator[AsyncClient]:
    async with make_client() as ac:
        yield ac


async def _scroll_payload_tokens(collection: str) -> list[str]:
    from qdrant_client import AsyncQdrantClient

    from app.config import get_settings

    settings = get_settings()
    client_q = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        scrolled, _ = await client_q.scroll(
            collection, limit=100, with_payload=True, with_vectors=False
        )
    finally:
        await client_q.close()
    assert scrolled
    tokens = {tuple(sorted((point.payload or {}).get("acl_tokens") or [])) for point in scrolled}
    assert len(tokens) == 1, f"payload acl_tokens 不一致：{tokens}"
    return list(next(iter(tokens)))


async def _wait_scan_pending(
    client: AsyncClient, uploader: dict[str, str], document_id: str, version_id: str
) -> None:
    """等扫描到 PENDING_APPROVAL；扫描 worker 不在测试进程内则直置（BS-03 同款）。"""

    import asyncio

    for _ in range(40):
        detail = await client.get(f"/api/v1/school/kb/documents/{document_id}", headers=uploader)
        if detail.json()["status"] == "PENDING_APPROVAL":
            return
        await asyncio.sleep(0.5)
    await execute(
        "update kb_document_versions set status = 'PENDING_APPROVAL', scan_engine = 'clamav',"
        " scanned_at = now() where id = :vid and status = 'UPLOADED'",
        vid=version_id,
    )
    await execute(
        "update kb_documents set status = 'PENDING_APPROVAL' where id = :doc and status = 'UPLOADED'",
        doc=document_id,
    )


async def test_bs_m3_09_acl_gate_and_publish_chain(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """未配置/未确认 409 → 确认 → 构建 READY + payload tokens → 发布 → 未确认 409 → 恢复。"""

    uploader = await login(client, UPLOADER)
    approver = await login(client, KB_APPROVER)
    manager = await login(client, KB_ACL_MANAGER)
    builder = await login(client, KB_BUILDER)
    publisher = await login(client, KB_PUBLISHER)

    # 1) 上传（dev-skip）→ 审批 → APPROVED
    response = await upload(
        client,
        uploader,
        key=f"bs09-{uuid4().hex[:16]}",
        files=[("acl-scenario.md", MARKDOWN.encode("utf-8"), "text/markdown")],
    )
    assert response.status_code == 201, response.text
    document = response.json()["documents"][0]
    version_id = document["current_version"]["id"]
    await _wait_scan_pending(client, uploader, str(document["id"]), version_id)
    submit = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/submit-approval", headers=uploader
    )
    assert submit.status_code == 201, submit.text
    approve = await client.post(
        f"/api/v1/school/approvals/{submit.json()['request_id']}/approve",
        headers=approver,
        json={},
    )
    assert approve.status_code == 200, approve.text

    # 2) 未配置 ACL → 构建 409 ACL_REQUIRED + 拒绝审计 + 零任务行
    denied = await client.post(BUILD_URL.format(version_id=version_id), headers=builder)
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "ACL_REQUIRED"
    assert (await rows("select count(*) as n from kb_build_jobs where version_id = :v", v=version_id))[
        0
    ]["n"] == 0
    build_denials = await rows(
        "select metadata_json from audit_logs where action = 'school.kb.build.denied' "
        "and tenant_id = :tid",
        tid=str(env.tenant_a),
    )
    assert any(a["metadata_json"]["code"] == "ACL_REQUIRED" for a in build_denials)

    # 3) 配置（department）未确认 → 构建 409 ACL_UNCONFIRMED
    department_id = uuid4()
    await execute(
        """
        insert into departments (id, tenant_id, name, path, status)
        values (:id, :t, :name, :path, 'active')
        """,
        id=str(department_id),
        t=str(env.tenant_a),
        name=f"教务处-{department_id.hex[:8]}",
        path=str(department_id),
    )
    put = await client.put(
        AUDIENCES_URL.format(version_id=version_id),
        headers=manager,
        json={"audiences": [{"audience_type": "department", "audience_id": str(department_id)}]},
    )
    assert put.status_code == 200, put.text
    denied2 = await client.post(BUILD_URL.format(version_id=version_id), headers=builder)
    assert denied2.status_code == 409, denied2.text
    assert denied2.json()["detail"]["code"] == "ACL_UNCONFIRMED"

    # 4) 确认 → 构建可达 → READY；payload 逐 chunk 携带 ACL token（验收 :321）
    confirm = await client.post(
        AUDIENCES_URL.format(version_id=version_id) + "/confirm", headers=manager
    )
    assert confirm.status_code == 200, confirm.text
    build = await client.post(BUILD_URL.format(version_id=version_id), headers=builder)
    assert build.status_code == 201, build.text
    patch_ports(monkeypatch, FakeParser(ParsedDocument(markdown=MARKDOWN)))
    result = await build_kb_version({}, str(env.tenant_a), version_id)
    assert result["status"] == "ready", result

    from app.kb.indexer import collection_name

    collection = collection_name(env.tenant_a, UUID(version_id))
    assert await _scroll_payload_tokens(collection) == [f"department:{department_id}"]

    # 5) 发布可达（TEST_PASSED 由本场景脚手架空档——发布门禁先过 ACL；直接置态）
    await execute(
        "update kb_document_versions set status = 'TEST_PASSED' where id = :id", id=version_id
    )
    await execute(
        "update kb_documents set status = 'TEST_PASSED' where id = :doc and "
        "current_version_id = :vid",
        doc=document["id"],
        vid=version_id,
    )
    publish = await client.post(PUBLISH_URL.format(version_id=version_id), headers=publisher)
    assert publish.status_code == 200, publish.text

    # 6) PUT 置未确认 → payload 立即清空（检索恒不召回 fail-closed）→ 再发布 409
    put2 = await client.put(
        AUDIENCES_URL.format(version_id=version_id),
        headers=manager,
        json={"audiences": [{"audience_type": "department", "audience_id": str(department_id)}]},
    )
    assert put2.status_code == 200, put2.text
    assert await _scroll_payload_tokens(collection) == []
    publish2 = await client.post(PUBLISH_URL.format(version_id=version_id), headers=publisher)
    assert publish2.status_code == 409, publish2.text
    assert publish2.json()["detail"]["code"] == "ACL_UNCONFIRMED"
    publish_denials = await rows(
        "select metadata_json from audit_logs where action = 'school.kb.publish.denied' "
        "and tenant_id = :tid",
        tid=str(env.tenant_a),
    )
    assert any(a["metadata_json"]["code"] == "ACL_UNCONFIRMED" for a in publish_denials)

    # 7) 重新确认 → 发布恢复（可重试语义）
    confirm2 = await client.post(
        AUDIENCES_URL.format(version_id=version_id) + "/confirm", headers=manager
    )
    assert confirm2.status_code == 200, confirm2.text
    assert await _scroll_payload_tokens(collection) == [f"department:{department_id}"]
    publish3 = await client.post(PUBLISH_URL.format(version_id=version_id), headers=publisher)
    assert publish3.status_code == 200, publish3.text

    # 8) 跨租 audiences 访问 404（B 租持 view 探测，不泄露存在性）
    b_publisher = await login(client, KB_B_PUBLISHER, school_code=SCHOOL_B)
    cross = await client.get(AUDIENCES_URL.format(version_id=version_id), headers=b_publisher)
    assert cross.status_code == 404, cross.text

    # 清理 Qdrant collection（测试数据不残留）
    from qdrant_client import AsyncQdrantClient

    from app.config import get_settings

    settings = get_settings()
    client_q = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        await client_q.delete_collection(collection)
    finally:
        await client_q.close()


async def test_bs_m3_09_direct_seed_audience_confirmed_build(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """直落库种子（configure_audiences）→ HTTP 构建 → READY：卡 4 夹具路径的业务级背书。"""

    uploader = await login(client, UPLOADER)
    approver = await login(client, KB_APPROVER)
    builder = await login(client, KB_BUILDER)

    response = await upload(
        client,
        uploader,
        key=f"bs09b-{uuid4().hex[:16]}",
        files=[("acl-seed.md", MARKDOWN.encode("utf-8"), "text/markdown")],
    )
    assert response.status_code == 201, response.text
    document = response.json()["documents"][0]
    version_id = document["current_version"]["id"]
    await _wait_scan_pending(client, uploader, str(document["id"]), version_id)
    submit = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/submit-approval", headers=uploader
    )
    assert submit.status_code == 201, submit.text
    approve = await client.post(
        f"/api/v1/school/approvals/{submit.json()['request_id']}/approve",
        headers=approver,
        json={},
    )
    assert approve.status_code == 200, approve.text

    await configure_audiences(env.tenant_a, UUID(version_id))
    build = await client.post(BUILD_URL.format(version_id=version_id), headers=builder)
    assert build.status_code == 201, build.text
    patch_ports(monkeypatch, FakeParser(ParsedDocument(markdown=MARKDOWN)))
    result = await build_kb_version({}, str(env.tenant_a), version_id)
    assert result["status"] == "ready", result

    from app.kb.indexer import collection_name

    collection = collection_name(env.tenant_a, UUID(version_id))
    assert await _scroll_payload_tokens(collection) == ["all"]

    from qdrant_client import AsyncQdrantClient

    from app.config import get_settings

    settings = get_settings()
    client_q = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        await client_q.delete_collection(collection)
    finally:
        await client_q.close()
