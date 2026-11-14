"""M3 KB-10A 切块编辑与派生重建集成测试（真实 PG + 真实 Qdrant；解析/嵌入 fake）。

覆盖简报 `docs/design-briefs/M3-KB-10.md` §3 契约：
- POST edits：201 提交态落库+审计 / 409 CHUNK_CONFLICT（base_hash 乐观锁）/ 422
  结构破坏与超长 / 404 跨租 chunk / 403 无 chunk.edit 码；
- GET chunk-edits：提交记录列表；
- derive：201（派生版本行/提交态 confirmed/其余 discarded/ACL 继承含确认态/
  BUILD_QUEUED/审计 derive_confirmed）/ 409 DERIVE_NO_EDITS / 409 CHUNK_EDIT_STATE；
- 派生构建管线：READY + **嵌入调用次数 = 改动块数**（D1-A 核心断言）+ 未改块向量
  从源 collection 复制在位 + payload is_human_edited + 源版本 collection 与 alias 不动；
- 并发编辑后者 409；派生版本 ACL 门禁（继承 confirmed 态 → 门禁天然过；手动清确认 → 409）。
"""

import os
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.kb.build_task import build_kb_version
from app.kb.indexer import collection_name
from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import (
    KB_CHUNK_CONFIRMER,
    KB_CHUNK_EDITOR,
    NO_PERM,
    UPLOADER,
    KbEnv,
    execute,
    kb_env,
    login,
    make_client,
    rows,
)
from tests.support.kb_publish_seed import (
    seed_test_passed_version as _seed_test_passed_version,
)
from tests.support.kb_retrieval_seed import TokenOverlapEmbedder

# 多章节（≥3 子块）——向量复用断言需要未改块（编辑 1 个、其余复用）
MULTI_CHUNK_MARKDOWN = """# 编辑样例手册

## 第一章 安装

安装步骤1完成。安装步骤2完成。安装步骤3完成。

## 第二章 配置

配置项1已生效。配置项2已生效。配置项3已生效。

## 第三章 常见问题

问题1已解决。问题2已解决。问题3已解决。
"""

pytestmark = [pytest.mark.integration]

EDITS_URL = "/api/v1/school/kb/chunks/{chunk_id}/edits"
EDIT_LIST_URL = "/api/v1/school/kb/document-versions/{version_id}/chunk-edits"
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


async def _child_chunks(version_id: UUID) -> list[dict[str, Any]]:
    return await rows(
        "select id, chunk_index, text, chunk_hash, parent_id from kb_chunks "
        "where document_version_id = :v and chunk_type = 'child' order by chunk_index",
        v=str(version_id),
    )


class CountingEmbedder(TokenOverlapEmbedder):
    """词袋 embedder + 调用文本计数（D1-A 核心断言：嵌入调用次数 = 改动块数）。"""

    def __init__(self) -> None:
        super().__init__()
        self.embedded_texts: list[str] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded_texts.extend(texts)
        return await super().embed(texts)


async def _seed_built_version(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> tuple[UUID, UUID, list[dict[str, Any]]]:
    """真实构建 READY → TEST_PASSED 版本（all 夹具已确认；词袋 embedder）。"""

    doc, version_id = await _seed_test_passed_version(
        env,
        monkeypatch,
        title="编辑样例文档",
        markdown=MULTI_CHUNK_MARKDOWN,
        embedder=TokenOverlapEmbedder(),
    )
    chunks = await _child_chunks(version_id)
    assert chunks
    return doc, version_id, chunks


# ---------------------------------------------------------------------------
# POST edits
# ---------------------------------------------------------------------------


async def test_submit_edit_201_persists_with_audit(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, _version_id, chunks = await _seed_built_version(env, monkeypatch)
    target = chunks[0]
    headers = await login(client, KB_CHUNK_EDITOR)

    new_content = target["text"] + "（已人工校正）"
    resp = await client.post(
        EDITS_URL.format(chunk_id=str(target["id"])),
        json={"new_content": new_content, "reason": "修正错别字", "base_hash": target["chunk_hash"]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "submitted"
    assert body["chunk_id"] == str(target["id"])

    records = await rows(
        "select status, before_hash, after_hash, edited_by from kb_chunk_edits "
        "where chunk_id = :c",
        c=str(target["id"]),
    )
    assert len(records) == 1
    assert records[0]["status"] == "submitted"
    assert records[0]["before_hash"] == target["chunk_hash"]

    audits = await rows(
        "select action, metadata_json from audit_logs where action = 'school.kb.chunk.edited' "
        "and resource_id = :c",
        c=str(target["id"]),
    )
    assert len(audits) == 1
    assert audits[0]["metadata_json"]["reason"] == "修正错别字"

    # M2：new_content 透出（KB-10B diff 渲染数据源）
    assert body["new_content"] == new_content


async def test_submit_edit_stale_base_hash_409(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, _version_id, chunks = await _seed_built_version(env, monkeypatch)
    target = chunks[0]
    headers = await login(client, KB_CHUNK_EDITOR)

    resp = await client.post(
        EDITS_URL.format(chunk_id=str(target["id"])),
        json={"new_content": "新内容", "reason": "并发编辑", "base_hash": "f" * 64},
        headers=headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "CHUNK_CONFLICT"
    # 零副作用：无提交记录
    records = await rows(
        "select id from kb_chunk_edits where chunk_id = :c", c=str(target["id"])
    )
    assert records == []


async def test_submit_edit_invalid_content_422(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, _version_id, chunks = await _seed_built_version(env, monkeypatch)
    target = chunks[0]
    headers = await login(client, KB_CHUNK_EDITOR)

    for payload_label, new_content in (
        ("围栏不配对", "```python\nprint(1)\n"),
        ("超长", "字" * 501),
    ):
        resp = await client.post(
            EDITS_URL.format(chunk_id=str(target["id"])),
            json={
                "new_content": new_content,
                "reason": "结构校验",
                "base_hash": target["chunk_hash"],
            },
            headers=headers,
        )
        assert resp.status_code == 422, f"{payload_label}：{resp.text}"
        assert resp.json()["detail"]["code"] == "CHUNK_CONTENT_INVALID"


async def test_submit_edit_already_submitted_409(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """同块已有 submitted（有效 base_hash——chunk_hash 未变）→ 409（审查 N8(a)）。"""

    _doc, _version_id, chunks = await _seed_built_version(env, monkeypatch)
    target = chunks[0]
    headers = await login(client, KB_CHUNK_EDITOR)

    resp1 = await client.post(
        EDITS_URL.format(chunk_id=str(target["id"])),
        json={
            "new_content": target["text"] + "（第一次）",
            "reason": "第一次提交",
            "base_hash": target["chunk_hash"],
        },
        headers=headers,
    )
    assert resp1.status_code == 201, resp1.text

    # chunk_hash 未变（编辑只落 edits 表不动源行）→ base_hash 仍有效 → 走 submitted 分支
    resp2 = await client.post(
        EDITS_URL.format(chunk_id=str(target["id"])),
        json={
            "new_content": target["text"] + "（第二次）",
            "reason": "第二次提交",
            "base_hash": target["chunk_hash"],
        },
        headers=headers,
    )
    assert resp2.status_code == 409, resp2.text
    assert resp2.json()["detail"]["code"] == "CHUNK_CONFLICT"


async def test_submit_edit_parent_chunk_rejected_422(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """父块编辑 → 422 CHUNK_EDIT_TARGET_INVALID（审查 M1/N8(c)：服务端拦截负例）。"""

    _doc, version_id, _chunks = await _seed_built_version(env, monkeypatch)
    parents = await rows(
        "select id, chunk_hash from kb_chunks where document_version_id = :v "
        "and chunk_type = 'parent' order by chunk_index limit 1",
        v=str(version_id),
    )
    assert parents
    target = parents[0]
    headers = await login(client, KB_CHUNK_EDITOR)

    resp = await client.post(
        EDITS_URL.format(chunk_id=str(target["id"])),
        json={
            "new_content": "父块文本（应被拒）",
            "reason": "父块编辑负例",
            "base_hash": target["chunk_hash"],
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "CHUNK_EDIT_TARGET_INVALID"
    denials = await rows(
        "select metadata_json from audit_logs where action = 'school.kb.chunk.edit_denied' "
        "and resource_id = :c",
        c=str(target["id"]),
    )
    assert len(denials) == 1


async def test_submit_edit_cross_tenant_chunk_404(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, _version_id, _chunks = await _seed_built_version(env, monkeypatch)
    headers = await login(client, KB_CHUNK_EDITOR)
    resp = await client.post(
        EDITS_URL.format(chunk_id=str(uuid4())),
        json={"new_content": "x", "reason": "跨租", "base_hash": "a" * 64},
        headers=headers,
    )
    assert resp.status_code == 404, resp.text


async def test_submit_edit_without_permission_403(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, _version_id, chunks = await _seed_built_version(env, monkeypatch)
    headers = await login(client, NO_PERM)
    resp = await client.post(
        EDITS_URL.format(chunk_id=str(chunks[0]["id"])),
        json={"new_content": "x", "reason": "无码", "base_hash": chunks[0]["chunk_hash"]},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text


async def test_concurrent_edit_second_with_stale_hash_409(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """并发编辑同一子块：第二个提交（base_hash 过期）409 且审计完整（spec :330）。"""

    _doc, _version_id, chunks = await _seed_built_version(env, monkeypatch)
    target = chunks[0]
    first = await login(client, KB_CHUNK_EDITOR)

    resp1 = await client.post(
        EDITS_URL.format(chunk_id=str(target["id"])),
        json={
            "new_content": target["text"] + "A",
            "reason": "编辑一",
            "base_hash": target["chunk_hash"],
        },
        headers=first,
    )
    assert resp1.status_code == 201, resp1.text

    resp2 = await client.post(
        EDITS_URL.format(chunk_id=str(target["id"])),
        json={"new_content": target["text"] + "B", "reason": "编辑二", "base_hash": target["chunk_hash"]},
        headers=first,
    )
    assert resp2.status_code == 409, resp2.text
    assert resp2.json()["detail"]["code"] == "CHUNK_CONFLICT"

    denials = await rows(
        "select metadata_json from audit_logs where action = 'school.kb.chunk.edit_denied' "
        "and resource_id = :c",
        c=str(target["id"]),
    )
    assert len(denials) == 1



# ---------------------------------------------------------------------------
# GET chunk-edits
# ---------------------------------------------------------------------------


async def test_list_chunk_edits(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id, chunks = await _seed_built_version(env, monkeypatch)
    editor = await login(client, KB_CHUNK_EDITOR)
    resp = await client.post(
        EDITS_URL.format(chunk_id=str(chunks[0]["id"])),
        json={
            "new_content": chunks[0]["text"] + "（校正）",
            "reason": "修正",
            "base_hash": chunks[0]["chunk_hash"],
        },
        headers=editor,
    )
    assert resp.status_code == 201, resp.text

    viewer = await login(client, UPLOADER)
    resp = await client.get(EDIT_LIST_URL.format(version_id=str(version_id)), headers=viewer)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"] if "items" in resp.json() else resp.json()
    assert len(items) == 1
    assert items[0]["status"] == "submitted"
    assert items[0]["reason"] == "修正"


# ---------------------------------------------------------------------------
# derive
# ---------------------------------------------------------------------------


async def _submit_edit(
    client: AsyncClient, chunk: dict[str, Any], suffix: str
) -> str:
    editor = await login(client, KB_CHUNK_EDITOR)
    resp = await client.post(
        EDITS_URL.format(chunk_id=str(chunk["id"])),
        json={
            "new_content": chunk["text"] + suffix,
            "reason": f"校正{suffix}",
            "base_hash": chunk["chunk_hash"],
        },
        headers=editor,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["after_hash"])


async def test_derive_without_edits_409(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id, _chunks = await _seed_built_version(env, monkeypatch)
    headers = await login(client, KB_CHUNK_CONFIRMER)
    resp = await client.post(DERIVE_URL.format(version_id=str(version_id)), headers=headers)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "DERIVE_NO_EDITS"


async def test_derive_201_full_semantics(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """derive 201：派生版本行 + 提交态翻转 + ACL 继承（含确认态）+ BUILD_QUEUED + 审计。"""

    doc, version_id, chunks = await _seed_built_version(env, monkeypatch)
    await _submit_edit(client, chunks[0], "（校正一）")

    confirmer = await login(client, KB_CHUNK_CONFIRMER)
    resp = await client.post(DERIVE_URL.format(version_id=str(version_id)), headers=confirmer)
    assert resp.status_code == 201, resp.text
    derived = resp.json()
    derived_version_id = derived["version_id"]
    assert derived["parent_version_id"] == str(version_id)
    assert derived["derivation_type"] == "chunk_edit"

    # 版本行：version_no = max+1、状态 APPROVED、BUILD_QUEUED 任务在位
    version_rows = await rows(
        "select version_no, status, derivation_type, parent_version_id, "
        "acl_confirmed_by, acl_confirmed_at from kb_document_versions where id = :v",
        v=derived_version_id,
    )
    assert version_rows[0]["version_no"] == 2
    assert version_rows[0]["status"] == "APPROVED"
    assert version_rows[0]["derivation_type"] == "chunk_edit"
    assert str(version_rows[0]["parent_version_id"]) == str(version_id)
    assert version_rows[0]["acl_confirmed_by"] is not None  # ACL 继承沿用已确认态（D3-A）
    assert version_rows[0]["acl_confirmed_at"] is not None

    jobs = await rows(
        "select status from kb_build_jobs where version_id = :v", v=derived_version_id
    )
    assert jobs[0]["status"] == "BUILD_QUEUED"

    # 提交态翻转：纳入的 confirmed + derived_version_id；源版本其余无提交不受影响
    edits = await rows(
        "select status, derived_version_id from kb_chunk_edits where source_version_id = :v",
        v=str(version_id),
    )
    assert edits[0]["status"] == "confirmed"
    assert str(edits[0]["derived_version_id"]) == derived_version_id

    # ACL 继承：audiences 行复制到派生版本（all 夹具）
    audiences = await rows(
        "select audience_type, audience_id from kb_document_audiences where version_id = :v",
        v=derived_version_id,
    )
    assert len(audiences) == 1
    assert audiences[0]["audience_type"] == "all"

    audits = await rows(
        "select metadata_json from audit_logs where action = 'school.kb.chunk.derive_confirmed' "
        "and resource_id = :v",
        v=str(version_id),
    )
    assert len(audits) == 1
    _ = doc


async def test_derive_repeat_confirmed_409(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id, chunks = await _seed_built_version(env, monkeypatch)
    await _submit_edit(client, chunks[0], "（校正）")
    confirmer = await login(client, KB_CHUNK_CONFIRMER)
    resp = await client.post(DERIVE_URL.format(version_id=str(version_id)), headers=confirmer)
    assert resp.status_code == 201, resp.text
    resp = await client.post(DERIVE_URL.format(version_id=str(version_id)), headers=confirmer)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "CHUNK_EDIT_STATE"


# ---------------------------------------------------------------------------
# 派生构建管线（D1-A 核心：嵌入调用次数 = 改动块数 + 未改块向量复制）
# ---------------------------------------------------------------------------


async def _derive_and_build(
    env: KbEnv,
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    version_id: UUID,
) -> tuple[UUID, CountingEmbedder]:
    await _submit_edit(client, (await _child_chunks(version_id))[0], "（校正）")
    confirmer = await login(client, KB_CHUNK_CONFIRMER)
    resp = await client.post(DERIVE_URL.format(version_id=str(version_id)), headers=confirmer)
    assert resp.status_code == 201, resp.text
    derived_version_id = UUID(resp.json()["version_id"])

    counter = CountingEmbedder()
    from app.kb import build_task

    monkeypatch.setattr(build_task, "_build_embedder", lambda settings: counter)
    result = await build_kb_version({}, str(env.tenant_a), str(derived_version_id))
    assert result["status"] == "ready", result
    return derived_version_id, counter


async def test_derived_build_ready_with_embedding_count_and_vector_copy(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """spec :330 验收：派生 READY 且仅改动块重嵌（嵌入调用次数 = 改动块数）；未改块向量复用。"""

    _doc, version_id, _unused_chunks = await _seed_built_version(env, monkeypatch)
    from qdrant_client import AsyncQdrantClient

    from app.config import get_settings

    settings = get_settings()
    source_client = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        source_points, _ = await source_client.scroll(
            collection_name(env.tenant_a, version_id), limit=100, with_vectors=True
        )
    finally:
        await source_client.close()
    assert source_points

    derived_version_id, counter = await _derive_and_build(
        env, monkeypatch, client, version_id
    )

    # 嵌入调用次数 = 改动块数（1 个子块编辑）
    assert len(counter.embedded_texts) == 1, counter.embedded_texts

    # 派生 collection：全量子块在位；未改块向量与源 collection 逐点一致（D1-A 复制）
    derived_client = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        derived_points, _ = await derived_client.scroll(
            collection_name(env.tenant_a, derived_version_id),
            limit=100,
            with_vectors=True,
            with_payload=True,
        )
    finally:
        await derived_client.close()
    assert derived_points
    derived_by_id = {str(point.id): point for point in derived_points}
    derived_by_hash = {
        str((point.payload or {}).get("text_hash")): point for point in derived_points
    }
    assert len(derived_by_id) == len(source_points)

    # 派生点 id 是新 uuid（KB-10A 设计：chunk 行新 id）——对照按 payload text_hash：
    # 未改块 chunk_hash 不变（向量逐值相等），改动块 hash = 编辑 after_hash（向量不同）
    source_by_hash = {
        str((point.payload or {}).get("text_hash")): point for point in source_points
    }
    # 改动块判定按 before_hash（源点 payload hash = 提交时的 before_hash；派生点 =
    # after_hash 新内容新 hash，源集合无同 hash 点）
    before_hashes = {
        str(r["before_hash"])
        for r in await rows(
            "select before_hash from kb_chunk_edits where source_version_id = :v and status = 'confirmed'",
            v=str(version_id),
        )
    }
    after_hashes = {
        str(r["after_hash"])
        for r in await rows(
            "select after_hash from kb_chunk_edits where source_version_id = :v and status = 'confirmed'",
            v=str(version_id),
        )
    }
    unchanged_copied = 0
    for text_hash, source_point in source_by_hash.items():
        if text_hash in before_hashes:
            # 改动块：派生集合无同 hash 点（新内容新 hash），is_human_edited=true 的点存在
            edited_points = [
                point
                for point in derived_points
                if (point.payload or {}).get("is_human_edited") is True
            ]
            assert len(edited_points) == 1
            assert derived_by_id and all(
                str((point.payload or {}).get("text_hash")) != text_hash
                for point in derived_points
            )
        else:
            # 未改块：chunk_hash 不变 → 派生集合有同 hash 点且向量逐值相等（D1-A 复制）
            derived_point = derived_by_hash.get(text_hash)
            assert derived_point is not None, f"未改块 hash {text_hash[:12]} 未复用"
            # 向量复用 = dense 逐值相等（sparse 按派生语料 IDF 重算——TD-312 口径）
            derived_dense = cast(dict[str, Any], derived_point.vector)["dense"]
            source_dense = cast(dict[str, Any], source_point.vector)["dense"]
            assert derived_dense == source_dense, (
                f"未改块 {text_hash[:12]} dense 向量未复用"
            )
            unchanged_copied += 1
    assert unchanged_copied == len(source_points) - 1

    # payload：is_human_edited 只在改动块 true（未改块 False；改动块 hash = after_hash）
    for point in derived_points:
        payload = point.payload or {}
        text_hash = str(payload.get("text_hash"))
        is_edited = text_hash in after_hashes
        assert payload.get("is_human_edited") is is_edited

    # 源版本 collection 与 alias 不动（spec :327）
    source_after, _ = await (lambda c: c)(
        AsyncQdrantClient(url=settings.qdrant_url)
    ).scroll(collection_name(env.tenant_a, version_id), limit=100)
    assert len(source_after) == len(source_points)


async def test_derived_version_without_confirmed_acl_build_409(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """派生版本 ACL 门禁同形：清确认列后构建 → 409 ACL_UNCONFIRMED（KB-09A 门禁同源）。"""

    _doc, version_id, _chunks = await _seed_built_version(env, monkeypatch)
    await _submit_edit(client, (await _child_chunks(version_id))[0], "（校正）")
    confirmer = await login(client, KB_CHUNK_CONFIRMER)
    resp = await client.post(DERIVE_URL.format(version_id=str(version_id)), headers=confirmer)
    assert resp.status_code == 201, resp.text
    derived_version_id = resp.json()["version_id"]

    # 模拟「继承失效」：清派生版本确认列（KB-09A 门禁语义在派生版本同样生效）
    await execute(
        "update kb_document_versions set acl_confirmed_by = null, acl_confirmed_at = null "
        "where id = :v",
        v=str(derived_version_id),
    )
    builder = await login(client, UPLOADER)
    # UPLOADER 无 build 码——用 KB_BUILDER？kb_env 有 KB_BUILDER（build+view）
    from tests.support.kb_env import KB_BUILDER

    builder = await login(client, KB_BUILDER)
    resp = await client.post(
        f"/api/v1/school/kb/document-versions/{derived_version_id}/build", headers=builder
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "ACL_UNCONFIRMED"
