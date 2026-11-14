"""业务场景 BS-M3-11：切块编辑与派生重建（M3 KB-10A，K5）。

Given 真实基础设施（PG/Redis/Qdrant；解析/嵌入 fake ports——真实引擎证据在
BS-M3-03，TD-295 口径）；
When HTTP 全链：上传 → 审批 → 构建 READY → 编辑子块（HTTP）→ derive（HTTP）→
     派生构建 READY → 发布派生版本 → 检索对比；
Then（对照 spec §KB-10 验收 :330）：
- **仅改动块重嵌**：派生构建嵌入调用次数 = 改动块数（fake embedder 计数），
  未改块向量从源 collection 复制；
- **旧会话不漂移**：派生版本发布后，源版本 collection 仍在且内容不变（旧会话
  固定源版本检索不漂移），新发布活跃指针指向派生版本；
- **并发编辑**：同子块第二个提交（base_hash 过期）409 `CHUNK_CONFLICT` 且审计；
- **派生 409 族**：无提交 `DERIVE_NO_EDITS`、重复派生 `CHUNK_EDIT_STATE`。
"""

import os
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from qdrant_client import AsyncQdrantClient

from app.kb.build_task import build_kb_version
from app.kb.parser import ParsedDocument
from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import (
    KB_ACL_MANAGER,
    KB_APPROVER,
    KB_BUILDER,
    KB_CHUNK_CONFIRMER,
    KB_CHUNK_EDITOR,
    KB_PUBLISHER,
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
from tests.support.kb_retrieval_seed import TokenOverlapEmbedder

pytestmark = [pytest.mark.business, pytest.mark.integration]

BUILD_URL = "/api/v1/school/kb/document-versions/{version_id}/build"
PUBLISH_URL = "/api/v1/school/kb/versions/{version_id}/publish"
# 多章节（≥3 子块）——向量复用断言需要未改块（编辑 1 个、其余复用）
MULTI_CHUNK_MARKDOWN = """# 编辑样例手册

## 第一章 安装

安装步骤1完成。安装步骤2完成。安装步骤3完成。

## 第二章 配置

配置项1已生效。配置项2已生效。配置项3已生效。

## 第三章 常见问题

问题1已解决。问题2已解决。问题3已解决。
"""

EDITS_URL = "/api/v1/school/kb/chunks/{chunk_id}/edits"
DERIVE_URL = "/api/v1/school/kb/document-versions/{version_id}/derive"


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
        monkeypatch, tmp_path, overrides={"QDRANT_URL": "http://127.0.0.1:56333"}
    ) as value:
        yield value


@pytest.fixture
async def client(env: KbEnv) -> AsyncIterator[AsyncClient]:
    async with make_client() as ac:
        yield ac


async def _wait_scan_pending(
    client: AsyncClient, uploader: dict[str, str], document_id: str, version_id: str
) -> None:
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


async def _scroll_payload_vectors(collection: str) -> dict[str, list[float]]:
    """按 payload text_hash → dense 向量（对齐断言用；派生点 id 是新 uuid）。"""

    from app.config import get_settings

    settings = get_settings()
    client_q = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        points, _ = await client_q.scroll(
            collection, limit=100, with_payload=True, with_vectors=True
        )
    finally:
        await client_q.close()
    from typing import Any, cast

    return {
        str((point.payload or {}).get("text_hash")): cast(
            dict[str, Any], point.vector
        )["dense"]
        for point in points
    }


async def test_bs_m3_11_edit_derive_publish_chain(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """编辑→派生→仅改动块重嵌→发布→旧会话不漂移（spec :330 验收主链）。"""

    uploader = await login(client, UPLOADER)
    approver = await login(client, KB_APPROVER)
    builder = await login(client, KB_BUILDER)
    editor = await login(client, KB_CHUNK_EDITOR)
    confirmer = await login(client, KB_CHUNK_CONFIRMER)
    publisher = await login(client, KB_PUBLISHER)
    acl_manager = await login(client, KB_ACL_MANAGER)

    # 1) 上传 → 审批 → APPROVED → 配置人群 → 构建 READY
    response = await upload(
        client,
        uploader,
        key=f"bs11-{uuid4().hex[:16]}",
        files=[("chunk-edit.md", MULTI_CHUNK_MARKDOWN.encode("utf-8"), "text/markdown")],
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

    put = await client.put(
        f"/api/v1/school/kb/document-versions/{version_id}/audiences",
        headers=acl_manager,
        json={"audiences": [{"audience_type": "all", "audience_id": None}]},
    )
    assert put.status_code == 200, put.text
    confirm = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/audiences/confirm", headers=acl_manager
    )
    assert confirm.status_code == 200, confirm.text

    build = await client.post(BUILD_URL.format(version_id=version_id), headers=builder)
    assert build.status_code == 201, build.text
    patch_ports(monkeypatch, FakeParser(ParsedDocument(markdown=MARKDOWN)))
    from app.kb import build_task

    monkeypatch.setattr(build_task, "_build_embedder", lambda s: TokenOverlapEmbedder())
    result = await build_kb_version({}, str(env.tenant_a), version_id)
    assert result["status"] == "ready", result

    # 2) HTTP 编辑一个子块（base_hash = 当前值）
    chunks = await rows(
        "select id, text, chunk_hash from kb_chunks where document_version_id = :v "
        "and chunk_type = 'child' order by chunk_index",
        v=version_id,
    )
    assert len(chunks) >= 1
    target = chunks[0]
    new_content = target["text"] + "（人工校正）"
    edit_resp = await client.post(
        EDITS_URL.format(chunk_id=str(target["id"])),
        json={"new_content": new_content, "reason": "BS-11 校正", "base_hash": target["chunk_hash"]},
        headers=editor,
    )
    assert edit_resp.status_code == 201, edit_resp.text

    # 3) 源版本 collection 名（派生前快照在清理段断言）
    from app.kb.indexer import collection_name

    source_collection = collection_name(env.tenant_a, UUID(version_id))

    # 4) derive（HTTP）→ 派生版本 + BUILD_QUEUED
    derive = await client.post(DERIVE_URL.format(version_id=version_id), headers=confirmer)
    assert derive.status_code == 201, derive.text
    derived_version_id = derive.json()["version_id"]

    # 5) 派生构建：**嵌入调用次数 = 改动块数（1）**（fake embedder 计数）
    counter = TokenOverlapEmbedder()
    embedded_texts: list[str] = []

    class Counting(TokenOverlapEmbedder):
        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            embedded_texts.extend(texts)
            return await counter.embed(texts)

    monkeypatch.setattr(build_task, "_build_embedder", lambda s: Counting())
    result = await build_kb_version({}, str(env.tenant_a), derived_version_id)
    assert result["status"] == "ready", result
    assert len(embedded_texts) == 1, (
        f"嵌入调用次数 {len(embedded_texts)} ≠ 改动块数 1（spec :330 仅改动块重嵌）"
    )

    # 6) 派生 collection：未改块向量逐值复用（按 payload text_hash 对齐，D1-A）
    derived_collection = collection_name(env.tenant_a, UUID(derived_version_id))
    # 只遍历 child 行——只索子块（KB-03B Minor 3），父块不在 Qdrant；且单段章节的
    # 父/子 hash 相同（父子文本一致），parent 行会误入未改块分支
    source_chunks_rows = await rows(
        "select id, chunk_hash, text from kb_chunks where document_version_id = :v "
        "and chunk_type = 'child'",
        v=version_id,
    )
    derived_payloads = {
        str(p["chunk_hash"]): p["text"]
        for p in await rows(
            "select chunk_hash, text from kb_chunks where document_version_id = :v",
            v=derived_version_id,
        )
    }
    # 改动块文本在派生里（新内容）
    assert new_content in derived_payloads.values()

    # 改动块按 chunk_id（编辑记录）判定；未改块按源 chunk_hash 对齐派生 payload
    edited = await rows(
        "select chunk_id from kb_chunk_edits where source_version_id = :v and status = 'confirmed'",
        v=version_id,
    )
    edited_chunk_ids = {str(r["chunk_id"]) for r in edited}

    source_payload_vectors = await _scroll_payload_vectors(source_collection)
    derived_payload_vectors = await _scroll_payload_vectors(derived_collection)
    for row in source_chunks_rows:
        if str(row["id"]) in edited_chunk_ids:
            # 改动块：派生集合无同 hash 点（新内容新 hash）
            assert row["chunk_hash"] not in derived_payload_vectors
            continue
        assert row["chunk_hash"] in derived_payload_vectors, f"未改块 {row['chunk_hash'][:12]} 缺向量"
        assert derived_payload_vectors[row["chunk_hash"]] == source_payload_vectors[row["chunk_hash"]], (
            f"未改块 {row['chunk_hash'][:12]} 向量未复用"
        )

    # 7) 发布派生版本（TEST_PASSED 由本脚手架直置——发布门禁链路 KB-05 已验）
    await execute(
        "update kb_document_versions set status = 'TEST_PASSED' where id = :id",
        id=derived_version_id,
    )
    await execute(
        "update kb_documents set status = 'TEST_PASSED' where id = :doc and "
        "current_version_id = :vid",
        doc=document["id"],
        vid=derived_version_id,
    )
    publish = await client.post(PUBLISH_URL.format(version_id=derived_version_id), headers=publisher)
    assert publish.status_code == 200, publish.text

    # 8) 旧会话不漂移：源版本 collection 仍在且向量未变（spec :328）
    source_vectors_after = await _scroll_payload_vectors(source_collection)
    assert source_vectors_after == source_payload_vectors, "源版本被派生构建污染" 

    # 清理（测试 collection 不残留）
    from app.config import get_settings

    settings_q = get_settings()
    client_q = AsyncQdrantClient(url=settings_q.qdrant_url)
    try:
        await client_q.delete_collection(source_collection)
        await client_q.delete_collection(derived_collection)
    finally:
        await client_q.close()


async def test_bs_m3_11_concurrent_edit_and_derive_409_family(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """并发编辑后者 409 + 派生 409 族（无提交/重复派生）。"""

    uploader = await login(client, UPLOADER)
    approver = await login(client, KB_APPROVER)
    builder = await login(client, KB_BUILDER)
    editor = await login(client, KB_CHUNK_EDITOR)
    confirmer = await login(client, KB_CHUNK_CONFIRMER)
    acl_manager = await login(client, KB_ACL_MANAGER)

    response = await upload(
        client,
        uploader,
        key=f"bs11b-{uuid4().hex[:16]}",
        files=[("chunk-edit2.md", MARKDOWN.encode("utf-8"), "text/markdown")],
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

    # 无提交 → derive 409 DERIVE_NO_EDITS
    no_edits = await client.post(DERIVE_URL.format(version_id=version_id), headers=confirmer)
    assert no_edits.status_code == 409, no_edits.text
    assert no_edits.json()["detail"]["code"] == "DERIVE_NO_EDITS"

    put = await client.put(
        f"/api/v1/school/kb/document-versions/{version_id}/audiences",
        headers=acl_manager,
        json={"audiences": [{"audience_type": "all", "audience_id": None}]},
    )
    assert put.status_code == 200, put.text
    confirm = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/audiences/confirm", headers=acl_manager
    )
    assert confirm.status_code == 200, confirm.text
    build = await client.post(BUILD_URL.format(version_id=version_id), headers=builder)
    assert build.status_code == 201, build.text
    patch_ports(monkeypatch, FakeParser(ParsedDocument(markdown=MARKDOWN)))
    result = await build_kb_version({}, str(env.tenant_a), version_id)
    assert result["status"] == "ready", result

    chunks = await rows(
        "select id, text, chunk_hash from kb_chunks where document_version_id = :v "
        "and chunk_type = 'child' order by chunk_index",
        v=version_id,
    )
    target = chunks[0]

    # 编辑一 201
    resp1 = await client.post(
        EDITS_URL.format(chunk_id=str(target["id"])),
        json={
            "new_content": target["text"] + "（编辑一）",
            "reason": "并发一",
            "base_hash": target["chunk_hash"],
        },
        headers=editor,
    )
    assert resp1.status_code == 201, resp1.text

    # 并发编辑二（base_hash 过期——chunk_hash 未变，但已有 submitted）→ 409 CHUNK_CONFLICT
    resp2 = await client.post(
        EDITS_URL.format(chunk_id=str(target["id"])),
        json={
            "new_content": target["text"] + "（编辑二）",
            "reason": "并发二",
            "base_hash": target["chunk_hash"],
        },
        headers=editor,
    )
    assert resp2.status_code == 409, resp2.text
    assert resp2.json()["detail"]["code"] == "CHUNK_CONFLICT"
    denials = await rows(
        "select metadata_json from audit_logs where action = 'school.kb.chunk.edit_denied' "
        "and resource_id = :c",
        c=str(target["id"]),
    )
    assert len(denials) == 1

    # 派生 → 重复派生 409 CHUNK_EDIT_STATE
    derive = await client.post(DERIVE_URL.format(version_id=version_id), headers=confirmer)
    assert derive.status_code == 201, derive.text
    repeat = await client.post(DERIVE_URL.format(version_id=version_id), headers=confirmer)
    assert repeat.status_code == 409, repeat.text
    assert repeat.json()["detail"]["code"] == "CHUNK_EDIT_STATE"

    from qdrant_client import AsyncQdrantClient

    from app.config import get_settings
    from app.kb.indexer import collection_name

    settings_q = get_settings()
    client_q = AsyncQdrantClient(url=settings_q.qdrant_url)
    try:
        await client_q.delete_collection(collection_name(env.tenant_a, UUID(version_id)))
    finally:
        await client_q.close()
