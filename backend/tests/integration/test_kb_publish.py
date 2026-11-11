"""M3 KB-05 发布/热更新集成测试（真实 PG + 真实 Qdrant；构建/嵌入 fake ports）。

覆盖简报 §4 红→绿清单：
- 发布：TEST_PASSED → 200（状态/指针行/索引行/审计/Qdrant alias/payload 全对账）；
- 门禁：未过测试 409 `KB_NOT_TEST_PASSED` + 拒绝审计 + 零指针行；
- 幂等重放：同一活跃版本再次发布 `replayed=true`，重建 alias 恢复路径；
- 回滚（D2-A）：切到任意 PUBLISHED 目标，两版本并存 PUBLISHED，alias/文档指针/审计对账；
- 回滚守卫：目标非 PUBLISHED 409、目标是活跃版本 409、无活跃发布 409；
- 下线：OFFLINE 终态 + 活跃指针撤除（fail-closed）+ collection 保留；
- 删除：DELETED 软删 + 指针撤除 + collection 保留（M6 清理）；
- 权限/跨租：无码 403；跨租发布 404（不泄露存在性）；
- 文档详情 `active_version_id`（KB-08A/TD-305）：发布/回滚/下线三态跟随 + 列表契约边界 + 跨租 404。

说明：TEST_PASSED 状态由共享脚手架直接落库（模拟 KB-04A 沙箱通过的可达终态——
沙箱自身的判分链路由 KB-04A 用例覆盖，本文件聚焦发布机制）。
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient, Response

from app.config import get_settings
from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import (
    KB_B_PUBLISHER,
    NO_PERM,
    SCHOOL_B,
    UPLOADER,
    VIEWER_SCHOOL,
    KbEnv,
    execute,
    kb_env,
    login,
    make_client,
    rows,
)
from tests.support.kb_publish_seed import (
    publisher_headers as _publisher_headers,
)
from tests.support.kb_publish_seed import (
    seed_test_passed_version as _seed_test_passed_version,
)
from tests.support.kb_publish_seed import (
    seed_two_published_versions as _seed_two_published_versions,
)

pytestmark = [pytest.mark.integration]


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


async def _publish(
    client: AsyncClient, headers: dict[str, str], version_id: UUID
) -> Response:
    return await client.post(
        f"/api/v1/school/kb/versions/{version_id}/publish", headers=headers
    )


async def _audit_rows(action: str, resource_id: UUID) -> list[dict[str, Any]]:
    return await rows(
        "select action, result, metadata_json from audit_logs "
        "where action = :action and resource_id = :rid order by sequence",
        action=action,
        rid=str(resource_id),
    )


async def _denied_audits(action: str, version_id: UUID) -> list[dict[str, Any]]:
    """拒绝审计按 metadata.version_id 查（denied 审计不写 resource_id——KB-02 惯例）。"""

    return await rows(
        "select action, result, metadata_json from audit_logs "
        "where action = :action and metadata_json->>'version_id' = :vid order by sequence",
        action=action,
        vid=str(version_id),
    )


# ---------------------------------------------------------------------------
# 发布
# ---------------------------------------------------------------------------


async def test_publish_switches_status_pointer_alias_and_audits(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """发布全对账：状态/文档/指针行/索引行/审计/Qdrant alias 与 payload。"""

    from qdrant_client import AsyncQdrantClient

    from app.kb.indexer import alias_name, collection_name
    from app.kb.service import active_publish

    doc_id, version_id = await _seed_test_passed_version(env, monkeypatch)
    headers = await _publisher_headers(client)

    response = await _publish(client, headers, version_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "PUBLISHED"
    assert body["replayed"] is False
    assert body["alias_synced"] is True
    assert body["previous_version_id"] is None
    assert body["collection"] == collection_name(env.tenant_a, version_id)
    assert body["alias"] == alias_name(env.tenant_a, doc_id)

    version = await rows(
        "select status from kb_document_versions where id = :id", id=str(version_id)
    )
    assert version[0]["status"] == "PUBLISHED"
    document = await rows(
        "select status, current_version_id from kb_documents where id = :id", id=str(doc_id)
    )
    assert document[0]["status"] == "PUBLISHED"
    assert str(document[0]["current_version_id"]) == str(version_id)

    pointer = await rows(
        "select alias, collection, version_id from kb_publish_aliases where document_id = :d",
        d=str(doc_id),
    )
    assert len(pointer) == 1
    assert pointer[0]["collection"] == collection_name(env.tenant_a, version_id)
    assert str(pointer[0]["version_id"]) == str(version_id)

    index_row = await rows(
        "select status, published_at from kb_index_versions where document_version_id = :v",
        v=str(version_id),
    )
    assert index_row[0]["status"] == "published"
    assert index_row[0]["published_at"] is not None

    audits = await _audit_rows("school.kb.published", version_id)
    assert len(audits) == 1
    assert audits[0]["result"] == "success"
    assert audits[0]["metadata_json"]["replayed"] is False

    # Qdrant：alias 指向新 collection + payload is_published 翻转
    settings = get_settings()
    qclient = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        aliases = await qclient.get_aliases()
        matched = [a for a in aliases.aliases if a.alias_name == alias_name(env.tenant_a, doc_id)]
        assert len(matched) == 1
        assert matched[0].collection_name == collection_name(env.tenant_a, version_id)
        points = await qclient.scroll(
            collection_name(env.tenant_a, version_id), with_payload=True, limit=5
        )
        assert points[0]
        assert all(p.payload and p.payload.get("is_published") is True for p in points[0])
    finally:
        await qclient.close()

    # M4 数据基础：活跃发布解析
    resolved = await active_publish(tenant_id=env.tenant_a, document_id=doc_id)
    assert resolved is not None
    assert resolved[0] == version_id
    assert resolved[1] == collection_name(env.tenant_a, version_id)


async def test_publish_requires_test_passed(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """未过测试（READY）发布 → 409 + 拒绝审计 + 零指针行。"""

    doc_id, version_id = await _seed_test_passed_version(env, monkeypatch)
    await execute(
        "update kb_document_versions set status = 'READY' where id = :id", id=str(version_id)
    )
    await execute("update kb_documents set status = 'READY' where id = :doc", doc=str(doc_id))
    headers = await _publisher_headers(client)

    response = await _publish(client, headers, version_id)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "KB_NOT_TEST_PASSED"

    pointer = await rows(
        "select id from kb_publish_aliases where document_id = :d", d=str(doc_id)
    )
    assert pointer == []
    audits = await _denied_audits("school.kb.publish.denied", version_id)
    assert len(audits) == 1
    assert audits[0]["metadata_json"]["code"] == "KB_NOT_TEST_PASSED"


async def test_publish_replay_is_idempotent_recovery_path(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """同一活跃版本重放发布：replayed=true、单指针行、alias 仍解析正确。"""

    from app.kb.service import active_publish

    doc_id, version_id = await _seed_test_passed_version(env, monkeypatch)
    headers = await _publisher_headers(client)

    first = await _publish(client, headers, version_id)
    assert first.status_code == 200, first.text
    assert first.json()["replayed"] is False

    second = await _publish(client, headers, version_id)
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True

    pointer = await rows(
        "select version_id from kb_publish_aliases where document_id = :d", d=str(doc_id)
    )
    assert len(pointer) == 1
    resolved = await active_publish(tenant_id=env.tenant_a, document_id=doc_id)
    assert resolved is not None and resolved[0] == version_id
    audits = await _audit_rows("school.kb.published", version_id)
    assert [a["metadata_json"]["replayed"] for a in audits] == [False, True]


async def test_publish_replay_rebuilds_missing_alias(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """alias 丢失（同步失败路径）后重放发布：alias 重建、alias_synced=true、解析不变。"""

    from qdrant_client import AsyncQdrantClient
    from qdrant_client import models as qmodels

    from app.kb.indexer import alias_name, collection_name
    from app.kb.service import active_publish

    doc_id, version_id = await _seed_test_passed_version(env, monkeypatch)
    headers = await _publisher_headers(client)
    first = await _publish(client, headers, version_id)
    assert first.status_code == 200, first.text
    assert first.json()["alias_synced"] is True

    alias = alias_name(env.tenant_a, doc_id)
    settings = get_settings()
    qclient = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        await qclient.update_collection_aliases(
            change_aliases_operations=[
                qmodels.DeleteAliasOperation(delete_alias=qmodels.DeleteAlias(alias_name=alias))
            ]
        )
        aliases = await qclient.get_aliases()
        assert all(a.alias_name != alias for a in aliases.aliases)
    finally:
        await qclient.close()

    replay = await _publish(client, headers, version_id)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["alias_synced"] is True

    qclient = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        aliases = await qclient.get_aliases()
        matched = [a for a in aliases.aliases if a.alias_name == alias]
        assert len(matched) == 1
        assert matched[0].collection_name == collection_name(env.tenant_a, version_id)
    finally:
        await qclient.close()
    resolved = await active_publish(tenant_id=env.tenant_a, document_id=doc_id)
    assert resolved is not None and resolved[0] == version_id


async def test_concurrent_first_publish_yields_single_pointer(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """并发首发布：恰一 replayed=false、一 replayed=true、指针行唯一、状态一致。"""

    doc_id, version_id = await _seed_test_passed_version(env, monkeypatch)
    headers = await _publisher_headers(client)

    responses = await asyncio.gather(
        *(_publish(client, headers, version_id) for _ in range(2))
    )
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    replayed = sorted(r.json()["replayed"] for r in responses)
    assert replayed == [False, True]

    pointer = await rows(
        "select version_id from kb_publish_aliases where document_id = :d", d=str(doc_id)
    )
    assert len(pointer) == 1
    version = await rows(
        "select status from kb_document_versions where id = :id", id=str(version_id)
    )
    assert version[0]["status"] == "PUBLISHED"


# ---------------------------------------------------------------------------
# 回滚
# ---------------------------------------------------------------------------


async def test_rollback_switches_to_target_and_keeps_both_published(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """D2-A：回滚 = 切到任意 PUBLISHED 目标；两版本并存；alias/指针/审计对账。"""

    from qdrant_client import AsyncQdrantClient

    from app.kb.indexer import alias_name, collection_name
    from app.kb.service import active_publish

    doc_id, v1, v2 = await _seed_two_published_versions(env, monkeypatch, client)
    headers = await _publisher_headers(client)

    response = await client.post(
        f"/api/v1/school/kb/versions/{v1}/rollback", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version_id"] == str(v1)
    assert body["previous_version_id"] == str(v2)
    assert body["collection"] == collection_name(env.tenant_a, v1)

    statuses = await rows(
        "select id, status from kb_document_versions where document_id = :d order by version_no",
        d=str(doc_id),
    )
    assert {str(s["id"]): s["status"] for s in statuses} == {
        str(v1): "PUBLISHED",
        str(v2): "PUBLISHED",
    }
    document = await rows(
        "select status, current_version_id from kb_documents where id = :d", d=str(doc_id)
    )
    assert document[0]["status"] == "PUBLISHED"
    assert str(document[0]["current_version_id"]) == str(v1)

    pointer = await rows(
        "select version_id, collection from kb_publish_aliases where document_id = :d",
        d=str(doc_id),
    )
    assert str(pointer[0]["version_id"]) == str(v1)
    assert pointer[0]["collection"] == collection_name(env.tenant_a, v1)

    audits = await _audit_rows("school.kb.rolled_back", v1)
    assert len(audits) == 1
    assert audits[0]["metadata_json"]["from_version_id"] == str(v2)

    settings = get_settings()
    qclient = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        aliases = await qclient.get_aliases()
        matched = [a for a in aliases.aliases if a.alias_name == alias_name(env.tenant_a, doc_id)]
        assert matched and matched[0].collection_name == collection_name(env.tenant_a, v1)
    finally:
        await qclient.close()

    resolved = await active_publish(tenant_id=env.tenant_a, document_id=doc_id)
    assert resolved is not None and resolved[0] == v1


async def test_rollback_guards(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """守卫：目标非 PUBLISHED 409 / 目标是活跃版本 409 / 无活跃发布 409。"""

    doc_id, v1, v2 = await _seed_two_published_versions(env, monkeypatch, client)
    headers = await _publisher_headers(client)

    # 目标非 PUBLISHED（新文档 READY）
    _, fresh_version = await _seed_test_passed_version(
        env, monkeypatch, title="未发布文档"
    )
    await execute(
        "update kb_document_versions set status = 'READY' where id = :id",
        id=str(fresh_version),
    )
    not_published = await client.post(
        f"/api/v1/school/kb/versions/{fresh_version}/rollback", headers=headers
    )
    assert not_published.status_code == 409
    assert not_published.json()["detail"]["code"] == "KB_NOT_PUBLISHED"

    # 目标是活跃版本（v2 当前活跃）
    self_rollback = await client.post(
        f"/api/v1/school/kb/versions/{v2}/rollback", headers=headers
    )
    assert self_rollback.status_code == 409
    assert self_rollback.json()["detail"]["code"] == "KB_ROLLBACK_TARGET_IS_ACTIVE"

    # 无活跃发布：软删活跃 v2（指针撤除）后回滚仍 PUBLISHED 的 v1
    deleted = await client.delete(f"/api/v1/school/kb/versions/{v2}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    no_active = await client.post(
        f"/api/v1/school/kb/versions/{v1}/rollback", headers=headers
    )
    assert no_active.status_code == 409
    assert no_active.json()["detail"]["code"] == "KB_NO_ACTIVE_PUBLISH"

    # 已发布但指针已撤除：重放发布不再误报「未过测试」（审查 m-4 独立错误码）
    replay_after_delete = await _publish(client, headers, v1)
    assert replay_after_delete.status_code == 409
    assert replay_after_delete.json()["detail"]["code"] == "KB_PUBLISHED_NOT_ACTIVE"

    pointers = await rows(
        "select document_id from kb_publish_aliases where document_id in (:a, :b)",
        a=str(doc_id),
        b=str(fresh_version),
    )
    assert pointers == []  # v2 删除撤指针，v1 回滚被拒未建指针


# ---------------------------------------------------------------------------
# 下线 / 删除
# ---------------------------------------------------------------------------


async def test_offline_removes_active_pointer_and_keeps_collection(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """下线：OFFLINE 终态 + 活跃指针撤除 + collection 保留（旧会话可继续引用）。"""

    from qdrant_client import AsyncQdrantClient

    from app.kb.indexer import collection_name
    from app.kb.service import active_publish

    doc_id, version_id = await _seed_test_passed_version(env, monkeypatch)
    headers = await _publisher_headers(client)
    published = await _publish(client, headers, version_id)
    assert published.status_code == 200, published.text

    response = await client.post(
        f"/api/v1/school/kb/versions/{version_id}/offline", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "OFFLINE"
    assert body["was_active"] is True

    version = await rows(
        "select status from kb_document_versions where id = :id", id=str(version_id)
    )
    assert version[0]["status"] == "OFFLINE"
    document = await rows(
        "select status, current_version_id from kb_documents where id = :d", d=str(doc_id)
    )
    assert document[0]["status"] == "OFFLINE"
    pointer = await rows(
        "select id from kb_publish_aliases where document_id = :d", d=str(doc_id)
    )
    assert pointer == []
    index_row = await rows(
        "select status, retired_at from kb_index_versions where document_version_id = :v",
        v=str(version_id),
    )
    assert index_row[0]["status"] == "retired"
    assert index_row[0]["retired_at"] is not None

    # fail-closed：无活跃发布可解析；collection 保留（旧会话按版本直查仍可用）
    assert await active_publish(tenant_id=env.tenant_a, document_id=doc_id) is None
    settings = get_settings()
    qclient = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        assert await qclient.collection_exists(collection_name(env.tenant_a, version_id))
    finally:
        await qclient.close()

    audits = await _audit_rows("school.kb.offlined", version_id)
    assert len(audits) == 1 and audits[0]["metadata_json"]["was_active"] is True

    # OFFLINE 是终态：不能再次发布
    republish = await _publish(client, headers, version_id)
    assert republish.status_code == 409
    assert republish.json()["detail"]["code"] == "KB_NOT_TEST_PASSED"


async def test_delete_soft_deletes_and_keeps_collection(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """删除：PUBLISHED → DELETED 软删 + 指针撤除 + collection 保留（M6 清理）。"""

    from qdrant_client import AsyncQdrantClient

    from app.kb.indexer import collection_name

    doc_id, version_id = await _seed_test_passed_version(env, monkeypatch)
    headers = await _publisher_headers(client)
    published = await _publish(client, headers, version_id)
    assert published.status_code == 200, published.text

    response = await client.delete(
        f"/api/v1/school/kb/versions/{version_id}", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "DELETED"
    assert body["was_active"] is True

    version = await rows(
        "select status from kb_document_versions where id = :id", id=str(version_id)
    )
    assert version[0]["status"] == "DELETED"
    document = await rows("select status from kb_documents where id = :d", d=str(doc_id))
    assert document[0]["status"] == "DELETED"
    pointer = await rows(
        "select id from kb_publish_aliases where document_id = :d", d=str(doc_id)
    )
    assert pointer == []

    settings = get_settings()
    qclient = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        assert await qclient.collection_exists(collection_name(env.tenant_a, version_id))
    finally:
        await qclient.close()

    audits = await _audit_rows("school.kb.deleted", version_id)
    assert len(audits) == 1 and audits[0]["metadata_json"]["was_active"] is True


# ---------------------------------------------------------------------------
# 权限与跨租
# ---------------------------------------------------------------------------


async def test_publish_permission_and_cross_tenant(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """无码 403（上传者/只读/无权限）；跨租发布 404 不泄露存在性。"""

    doc_id, version_id = await _seed_test_passed_version(env, monkeypatch)

    uploader_headers = await login(client, UPLOADER)
    forbidden = await _publish(client, uploader_headers, version_id)
    assert forbidden.status_code == 403

    viewer_headers = await login(client, VIEWER_SCHOOL)
    assert (await _publish(client, viewer_headers, version_id)).status_code == 403

    noperm_headers = await login(client, NO_PERM)
    assert (await _publish(client, noperm_headers, version_id)).status_code == 403

    b_headers = await login(client, KB_B_PUBLISHER, SCHOOL_B)
    cross = await _publish(client, b_headers, version_id)
    assert cross.status_code == 404
    pointer = await rows(
        "select id from kb_publish_aliases where document_id = :d", d=str(doc_id)
    )
    assert pointer == []

    # 有码发布人（A 租）可达
    headers = await _publisher_headers(client)
    ok = await _publish(client, headers, version_id)
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------------
# 文档详情 active_version_id（KB-08A/TD-305：版本切换 MVP 的服真化面）
# ---------------------------------------------------------------------------


async def _detail(client: AsyncClient, headers: dict[str, str], document_id: UUID) -> Response:
    return await client.get(f"/api/v1/school/kb/documents/{document_id}", headers=headers)


async def test_detail_active_version_id_null_before_publish(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """未发布文档：详情回包 active_version_id 为 null（fail-closed，不误标）。"""

    doc_id, _version_id = await _seed_test_passed_version(env, monkeypatch)
    headers = await _publisher_headers(client)

    body = (await _detail(client, headers, doc_id)).json()
    assert body["active_version_id"] is None


async def test_detail_active_version_id_follows_publish(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """发布后详情：active_version_id 与指针行同源（库行是事实来源）。"""

    doc_id, version_id = await _seed_test_passed_version(env, monkeypatch)
    headers = await _publisher_headers(client)
    ok = await _publish(client, headers, version_id)
    assert ok.status_code == 200, ok.text

    body = (await _detail(client, headers, doc_id)).json()
    assert body["active_version_id"] == str(version_id)


async def test_detail_active_version_id_follows_rollback_to_old_version(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """回滚到旧版本后详情：active_version_id = 旧版本（spec §KB-08 :302 前半）。"""

    doc_id, v1, _v2 = await _seed_two_published_versions(env, monkeypatch, client)
    headers = await _publisher_headers(client)

    rollback = await client.post(f"/api/v1/school/kb/versions/{v1}/rollback", headers=headers)
    assert rollback.status_code == 200, rollback.text

    body = (await _detail(client, headers, doc_id)).json()
    assert body["active_version_id"] == str(v1)


async def test_detail_active_version_id_null_after_offline(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """下线活跃版本（撤指针）后详情：active_version_id 回到 null（fail-closed）。"""

    doc_id, version_id = await _seed_test_passed_version(env, monkeypatch)
    headers = await _publisher_headers(client)
    assert (await _publish(client, headers, version_id)).status_code == 200

    offline = await client.post(
        f"/api/v1/school/kb/versions/{version_id}/offline", headers=headers
    )
    assert offline.status_code == 200, offline.text

    body = (await _detail(client, headers, doc_id)).json()
    assert body["active_version_id"] is None


async def test_document_list_items_lack_active_version_id(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """契约边界：列表回包不携带 active_version_id（详情专属字段）。"""

    from app.api.school_kb import KbDocumentDetailResponse, KbDocumentResponse

    doc_id, _version_id = await _seed_test_passed_version(env, monkeypatch)
    headers = await _publisher_headers(client)

    response = await client.get("/api/v1/school/kb/documents", headers=headers)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert any(item["id"] == str(doc_id) for item in items), "种子文档应在 school 范围列表内"
    assert all("active_version_id" not in item for item in items)
    # 根锚点：字段只允许存在于 Detail 子类——基模型一旦带上该字段，
    # 列表/上传/PUT 元数据三处基模型回包契约全部破（审查 M-2）
    assert "active_version_id" not in KbDocumentResponse.model_fields
    assert "active_version_id" in KbDocumentDetailResponse.model_fields


async def test_detail_cross_tenant_404(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """跨租详情 404 不泄露存在性（同文、无 active 字段可泄露）。"""

    doc_id, _version_id = await _seed_test_passed_version(env, monkeypatch)
    headers = await _publisher_headers(client)
    assert (await _detail(client, headers, doc_id)).status_code == 200

    b_headers = await login(client, KB_B_PUBLISHER, SCHOOL_B)
    cross = await _detail(client, b_headers, doc_id)
    assert cross.status_code == 404
    assert cross.json()["detail"]["code"] == "DOCUMENT_NOT_FOUND"
    assert cross.json()["detail"]["message"] == "文档不存在"
