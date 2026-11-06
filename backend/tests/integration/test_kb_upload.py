"""M3 KB-01A 上传接口集成测试（真实 PG + 本地对象存储）。

覆盖简报 §4 红→绿清单中服务/接口部分：
- 1 批次 + N 文档/版本落库、对象 key 与哈希、审计 `school.kb.documents.uploaded`；
- 整批原子（任一文件不合格 → 422 且库/存储零残留，写 `school.kb.upload.denied`）；
- 幂等键重放（同键同指纹）与复用冲突（换文件/换创建人 → 409）；
- 数据范围（self 只见自己、school 见全校）与跨租 404；
- 权限（无 upload 403、只有 upload 不能看列表）；
- 分页游标（默认 20/上限 100、非法游标 400）。
"""

import hashlib
import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient

from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import (
    B_UPLOADER,
    MD,
    NO_PERM,
    PDF,
    SCHOOL_B,
    TXT,
    UPLOAD_ONLY,
    UPLOADER,
    UPLOADER_2,
    VIEWER_SCHOOL,
    VIEWER_SELF,
    KbEnv,
    kb_env,
    login,
    make_client,
    object_files,
    rows,
    upload,
)

pytestmark = [pytest.mark.integration]


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
async def env(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[KbEnv]:
    """清空业务数据、种两所学校账号，并把应用指向测试栈与临时对象目录。"""

    async with kb_env(monkeypatch, tmp_path) as value:
        yield value


@pytest.fixture
async def client(env: KbEnv) -> AsyncIterator[AsyncClient]:
    async with make_client() as ac:
        yield ac


async def test_upload_persists_batch_documents_versions_objects_and_audit(
    client: AsyncClient, env: KbEnv
) -> None:
    """一次上传 2 个文件：批次/文档/版本/对象/审计全部对得上。"""

    headers = await login(client, UPLOADER)
    key = f"batch-{uuid4().hex}"
    metadata = json.dumps(
        [{"title": "校园安全手册", "tags": ["安全", "手册"], "description": "2026 版"}, {}],
        ensure_ascii=False,
    )

    response = await upload(
        client,
        headers,
        key=key,
        files=[
            ("校园安全.pdf", PDF, "application/pdf"),
            ("校规.md", MD, "text/markdown"),
        ],
        metadata=metadata,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["replayed"] is False
    assert len(body["documents"]) == 2
    first, second = body["documents"]
    assert first["title"] == "校园安全手册"
    assert first["tags"] == ["安全", "手册"]
    assert first["description"] == "2026 版"
    assert second["title"] == "校规"
    assert second["tags"] == []
    assert first["status"] == "UPLOADED"
    assert first["current_version"]["scan"]["state"] == "pending"
    assert first["current_version"]["filename"] == "校园安全.pdf"
    assert first["current_version"]["mime"] == "application/pdf"
    assert first["current_version"]["size_bytes"] == len(PDF)
    assert second["current_version"]["mime"] == "text/markdown"
    assert second["current_version"]["version_no"] == 1

    batches = await rows("select * from kb_upload_batches where idempotency_key = :key", key=key)
    assert len(batches) == 1
    batch = batches[0]
    assert batch["tenant_id"] == env.tenant_a
    assert batch["file_count"] == 2
    assert batch["total_bytes"] == len(PDF) + len(MD)

    documents = await rows(
        "select * from kb_documents order by upload_file_index asc"
    )
    assert len(documents) == 2
    versions = await rows("select * from kb_document_versions")
    assert len(versions) == 2
    version_by_document = {version["document_id"]: version for version in versions}
    for document in documents:
        version = version_by_document[document["id"]]
        assert document["current_version_id"] == version["id"]
        assert document["status"] == version["status"] == "UPLOADED"
        assert document["upload_batch_id"] == batch["id"]
        assert version["created_by"] == document["created_by"]
        assert version["scan_attempts"] == 0
        assert version["scan_engine"] is None
        stored = env.object_dir / version["source_object_key"]
        assert stored.is_file()
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == version["source_hash"]
        assert version["source_object_key"].startswith(
            f"kb/{env.tenant_a}/{document['id']}/{version['id']}/source"
        )

    audits = await rows(
        "select * from audit_logs where action = 'school.kb.documents.uploaded'"
    )
    assert len(audits) == 1
    assert audits[0]["metadata_json"]["file_count"] == 2
    assert audits[0]["metadata_json"]["extensions"] == [".md", ".pdf"]
    assert audits[0]["request_id"]

    detail = await client.get(
        f"/api/v1/school/kb/documents/{first['id']}", headers=headers
    )
    assert detail.status_code == 200
    assert detail.json()["id"] == first["id"]

    versions_response = await client.get(
        f"/api/v1/school/kb/documents/{first['id']}/versions", headers=headers
    )
    assert versions_response.status_code == 200
    version_items = versions_response.json()["items"]
    assert len(version_items) == 1
    assert version_items[0]["scan"]["state"] == "pending"


async def test_batch_is_atomic_when_one_file_invalid(
    client: AsyncClient, env: KbEnv
) -> None:
    """一个文件伪装成 docx：整批 422，库与对象存储零残留，写拒绝审计。"""

    headers = await login(client, UPLOADER)
    response = await upload(
        client,
        headers,
        key=f"atomic-{uuid4().hex}",
        files=[
            ("ok.pdf", PDF, "application/pdf"),
            ("伪装.docx", b"not a zip", "application/vnd.openxmlformats-"
             "officedocument.wordprocessingml.document"),
        ],
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "BATCH_VALIDATION_FAILED"
    assert len(detail["files"]) == 1
    assert detail["files"][0]["index"] == 1
    assert detail["files"][0]["filename"] == "伪装.docx"
    assert detail["files"][0]["code"] == "ZIP_INVALID"

    assert (await rows("select * from kb_upload_batches")) == []
    assert (await rows("select * from kb_documents")) == []
    assert (await rows("select * from kb_document_versions")) == []
    assert object_files(env.object_dir) == []

    denied = await rows(
        "select * from audit_logs where action = 'school.kb.upload.denied'"
    )
    assert len(denied) == 1
    assert denied[0]["result"] == "denied"
    assert denied[0]["metadata_json"]["codes"] == ["ZIP_INVALID"]
    assert denied[0]["metadata_json"]["file_count"] == 2


async def test_object_store_failure_leaves_no_rows_or_objects(
    client: AsyncClient, env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二个对象写失败：接口 503，已写对象被清理，库中零残留。"""

    from app.kb import service as kb_service
    from app.storage.objects import LocalObjectStore, ObjectStoreError

    class FlakyStore(LocalObjectStore):
        """第一次写入成功、第二次失败的本地存储。"""

        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.calls = 0

        async def put(self, key: str, data: bytes, content_type: str) -> None:
            self.calls += 1
            if self.calls == 2:
                raise ObjectStoreError("模拟对象存储故障")
            await super().put(key, data, content_type)

    store = FlakyStore(env.object_dir)
    monkeypatch.setattr(kb_service, "build_object_store", lambda _settings: store)

    headers = await login(client, UPLOADER)
    response = await upload(
        client,
        headers,
        key=f"store-{uuid4().hex}",
        files=[
            ("a.pdf", PDF, "application/pdf"),
            ("b.md", MD, "text/markdown"),
        ],
    )
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "OBJECT_STORE_UNAVAILABLE"
    assert (await rows("select * from kb_upload_batches")) == []
    assert (await rows("select * from kb_documents")) == []
    assert (await rows("select * from kb_document_versions")) == []
    # 第一个文件已写入，失败后必须删除（不留无主对象）
    assert object_files(env.object_dir) == []


async def test_idempotency_replay_returns_same_batch(client: AsyncClient) -> None:
    """同键同指纹重试：返回原批次，不重复建档。"""

    headers = await login(client, UPLOADER)
    key = f"replay-{uuid4().hex}"
    files = [("a.pdf", PDF, "application/pdf")]
    first = await upload(client, headers, key=key, files=files)
    assert first.status_code == 201, first.text
    first_body = first.json()

    second = await upload(client, headers, key=key, files=files)
    assert second.status_code == 201, second.text
    second_body = second.json()
    assert second_body["replayed"] is True
    assert second_body["batch_id"] == first_body["batch_id"]
    assert [doc["id"] for doc in second_body["documents"]] == [
        doc["id"] for doc in first_body["documents"]
    ]
    assert len(await rows("select * from kb_upload_batches")) == 1
    assert len(await rows("select * from kb_documents")) == 1
    assert len(await rows("select * from kb_document_versions")) == 1


async def test_idempotency_conflict_when_files_differ(
    client: AsyncClient, env: KbEnv
) -> None:
    """同键换文件：409 IDEMPOTENCY_KEY_REUSED，且不写对象、不重复建档。"""

    headers = await login(client, UPLOADER)
    key = f"conflict-{uuid4().hex}"
    first = await upload(
        client, headers, key=key, files=[("a.pdf", PDF, "application/pdf")]
    )
    assert first.status_code == 201, first.text

    second = await upload(
        client, headers, key=key, files=[("b.pdf", PDF + b"more", "application/pdf")]
    )
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert len(await rows("select * from kb_documents")) == 1
    # 冲突在写对象之前就返回：存储里仍只有第一次上传的那一个对象
    assert len(object_files(env.object_dir)) == 1

    denied = await rows(
        "select * from audit_logs where action = 'school.kb.upload.denied'"
    )
    assert len(denied) == 1
    assert denied[0]["metadata_json"]["reason"] == "idempotency_conflict"


def _force_precheck_miss(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """让下一次上传的幂等预检落空，强制走「并发同键撞唯一约束」分支。"""

    from app.kb import service as kb_service

    original = kb_service._find_batch_by_key
    calls = {"count": 0}

    async def wrapper(tenant_id: Any, idempotency_key: str) -> Any:
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return await original(tenant_id, idempotency_key)

    monkeypatch.setattr(kb_service, "_find_batch_by_key", wrapper)
    return calls


async def test_idempotency_race_replays_after_unique_violation(
    client: AsyncClient, env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发同键：后到请求预检落空、撞库后按幂等重放，且清理自己已写的对象。"""

    headers = await login(client, UPLOADER)
    key = f"race-{uuid4().hex}"
    files = [("a.pdf", PDF, "application/pdf")]
    first = await upload(client, headers, key=key, files=files)
    assert first.status_code == 201, first.text
    winner = first.json()

    calls = _force_precheck_miss(monkeypatch)
    second = await upload(client, headers, key=key, files=files)
    assert second.status_code == 201, second.text
    body = second.json()
    # 预检 + 撞库后的复查：确实进入了 IntegrityError 分支
    assert calls["count"] == 2
    assert body["replayed"] is True
    assert body["batch_id"] == winner["batch_id"]
    assert [doc["id"] for doc in body["documents"]] == [
        doc["id"] for doc in winner["documents"]
    ]

    assert len(await rows("select * from kb_upload_batches")) == 1
    assert len(await rows("select * from kb_documents")) == 1
    assert len(await rows("select * from kb_document_versions")) == 1
    # 输家先写了一个对象，撞库后必须删掉，只剩赢家的那一个
    assert len(object_files(env.object_dir)) == 1
    uploaded = await rows(
        "select * from audit_logs where action = 'school.kb.documents.uploaded'"
    )
    assert len(uploaded) == 1


async def test_idempotency_race_with_different_files_conflicts(
    client: AsyncClient, env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发同键不同文件：撞库后 409，输家对象被清理、原批次不受影响。"""

    headers = await login(client, UPLOADER)
    key = f"race-conflict-{uuid4().hex}"
    first = await upload(
        client, headers, key=key, files=[("a.pdf", PDF, "application/pdf")]
    )
    assert first.status_code == 201, first.text

    calls = _force_precheck_miss(monkeypatch)
    second = await upload(
        client, headers, key=key, files=[("b.pdf", PDF + b"more", "application/pdf")]
    )
    assert second.status_code == 409, second.text
    assert calls["count"] == 2
    assert second.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert len(await rows("select * from kb_documents")) == 1
    assert len(object_files(env.object_dir)) == 1
    denied = await rows(
        "select * from audit_logs where action = 'school.kb.upload.denied'"
    )
    assert len(denied) == 1
    assert denied[0]["metadata_json"]["reason"] == "idempotency_conflict"


async def test_idempotency_conflict_when_creator_differs(client: AsyncClient) -> None:
    """同租户另一上传者复用同键：409（不泄露原批次内容）。"""

    headers = await login(client, UPLOADER)
    key = f"other-{uuid4().hex}"
    files = [("a.pdf", PDF, "application/pdf")]
    assert (await upload(client, headers, key=key, files=files)).status_code == 201

    other = await login(client, UPLOADER_2)
    response = await upload(client, other, key=key, files=files)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_idempotency_key_required_and_format_checked(client: AsyncClient) -> None:
    """缺键由 FastAPI 422；键格式非法由服务层 422 并写拒绝审计。"""

    headers = await login(client, UPLOADER)
    missing = await client.post(
        "/api/v1/school/kb/documents",
        headers=headers,
        files=[("files", ("a.pdf", PDF, "application/pdf"))],
    )
    assert missing.status_code == 422

    short = await upload(
        client, headers, key="short", files=[("a.pdf", PDF, "application/pdf")]
    )
    assert short.status_code == 422
    assert short.json()["detail"]["code"] == "INVALID_IDEMPOTENCY_KEY"
    denied = await rows(
        "select * from audit_logs where action = 'school.kb.upload.denied'"
    )
    assert len(denied) == 1
    assert denied[0]["metadata_json"]["reason"] == "invalid_idempotency_key"


async def test_self_scope_sees_only_own_uploads(client: AsyncClient) -> None:
    """self 范围只看到自己上传的；school 范围看到全校；他人文档 404。"""

    uploader = await login(client, UPLOADER)
    uploaded = await upload(
        client, uploader, key=f"scope-{uuid4().hex}",
        files=[("a.pdf", PDF, "application/pdf")],
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["documents"][0]["id"]

    self_viewer = await login(client, VIEWER_SELF)
    self_list = await client.get("/api/v1/school/kb/documents", headers=self_viewer)
    assert self_list.status_code == 200
    assert self_list.json()["items"] == []
    assert (
        await client.get(f"/api/v1/school/kb/documents/{document_id}", headers=self_viewer)
    ).status_code == 404
    assert (
        await client.get(
            f"/api/v1/school/kb/documents/{document_id}/versions", headers=self_viewer
        )
    ).status_code == 404

    school_viewer = await login(client, VIEWER_SCHOOL)
    school_list = await client.get("/api/v1/school/kb/documents", headers=school_viewer)
    assert school_list.status_code == 200
    assert [item["id"] for item in school_list.json()["items"]] == [document_id]
    assert (
        await client.get(f"/api/v1/school/kb/documents/{document_id}", headers=school_viewer)
    ).status_code == 200


async def test_cross_tenant_document_is_404(client: AsyncClient) -> None:
    """B 校上传的文档，A 校全校范围查看仍 404（RLS + 仓储租户过滤）。"""

    b_headers = await login(client, B_UPLOADER, SCHOOL_B)
    uploaded = await upload(
        client, b_headers, key=f"cross-{uuid4().hex}",
        files=[("b.pdf", PDF, "application/pdf")],
    )
    assert uploaded.status_code == 201, uploaded.text
    b_document_id = uploaded.json()["documents"][0]["id"]

    a_headers = await login(client, VIEWER_SCHOOL)
    assert (
        await client.get(f"/api/v1/school/kb/documents/{b_document_id}", headers=a_headers)
    ).status_code == 404
    a_list = await client.get("/api/v1/school/kb/documents", headers=a_headers)
    assert a_list.json()["items"] == []


async def test_permissions_upload_and_view_are_separate(client: AsyncClient) -> None:
    """无 upload 权限 403；只有 upload 的账号不能看列表（教师最小权限）。"""

    no_perm = await login(client, NO_PERM)
    denied = await upload(
        client, no_perm, key=f"noperm-{uuid4().hex}",
        files=[("a.pdf", PDF, "application/pdf")],
    )
    assert denied.status_code == 403

    upload_only = await login(client, UPLOAD_ONLY)
    uploaded = await upload(
        client, upload_only, key=f"uploadonly-{uuid4().hex}",
        files=[("a.pdf", PDF, "application/pdf")],
    )
    assert uploaded.status_code == 201, uploaded.text
    listing = await client.get("/api/v1/school/kb/documents", headers=upload_only)
    assert listing.status_code == 403


async def test_list_pagination_cursor_and_invalid_cursor(client: AsyncClient) -> None:
    """列表按游标分页不重不漏；非法游标 400。"""

    headers = await login(client, UPLOADER)
    uploaded = await upload(
        client,
        headers,
        key=f"page-{uuid4().hex}",
        files=[
            ("a.pdf", PDF, "application/pdf"),
            ("b.md", MD, "text/markdown"),
            ("c.txt", TXT, "text/plain"),
        ],
    )
    assert uploaded.status_code == 201, uploaded.text

    first = await client.get(
        "/api/v1/school/kb/documents", headers=headers, params={"limit": 2}
    )
    assert first.status_code == 200
    first_body = first.json()
    assert len(first_body["items"]) == 2
    assert first_body["next_cursor"]

    second = await client.get(
        "/api/v1/school/kb/documents",
        headers=headers,
        params={"limit": 2, "cursor": first_body["next_cursor"]},
    )
    assert second.status_code == 200
    second_body = second.json()
    assert len(second_body["items"]) == 1
    assert second_body["next_cursor"] is None
    ids = {item["id"] for item in first_body["items"]} | {
        item["id"] for item in second_body["items"]
    }
    assert len(ids) == 3

    invalid = await client.get(
        "/api/v1/school/kb/documents", headers=headers, params={"cursor": "not-a-cursor"}
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "INVALID_CURSOR"

    # 上限 100：101 被参数校验拒绝
    assert (
        await client.get(
            "/api/v1/school/kb/documents", headers=headers, params={"limit": 101}
        )
    ).status_code == 422


async def test_dev_skip_file_scan_marks_pending_approval(
    migrated_database: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开发旁路：DEV_SKIP_FILE_SCAN=true 时上传即 PENDING_APPROVAL，不入扫描队列。"""

    from app.config import get_settings
    from app.kb.status import assert_submittable
    from app.workers.settings import create_producer_pool, queue_name

    async with (
        kb_env(monkeypatch, tmp_path, overrides={"DEV_SKIP_FILE_SCAN": "true"}) as env,
        make_client() as dev_client,
    ):
        headers = await login(dev_client, UPLOADER)
        response = await upload(
            dev_client,
            headers,
            key=f"devskip-{uuid4().hex}",
            files=[("a.pdf", PDF, "application/pdf")],
        )
        assert response.status_code == 201, response.text
        document = response.json()["documents"][0]
        assert document["status"] == "PENDING_APPROVAL"
        assert document["current_version"]["scan"]["state"] == "skipped"
        assert document["current_version"]["scan"]["engine"] == "dev-skip"
        assert_submittable(document["status"])

        versions = await rows("select * from kb_document_versions")
        assert versions[0]["status"] == "PENDING_APPROVAL"
        assert versions[0]["scan_engine"] == "dev-skip"
        assert versions[0]["scanned_at"] is not None

        settings = get_settings()
        pool = await create_producer_pool(settings)
        try:
            assert await pool.zcard(queue_name(settings)) == 0
        finally:
            await pool.aclose()

        audits = await rows(
            "select * from audit_logs where action in "
            "('school.kb.documents.uploaded', 'school.kb.scan.passed') order by action"
        )
        assert [row["action"] for row in audits] == [
            "school.kb.documents.uploaded",
            "school.kb.scan.passed",
        ]
        assert audits[1]["metadata_json"] == {"engine": "dev-skip"}
        assert object_files(env.object_dir) != []


async def test_idempotency_conflict_when_metadata_differs(
    client: AsyncClient, env: KbEnv
) -> None:
    """TD-277：同键同文件但元数据不同 → 409，不再把元数据改动静默吞掉。"""

    headers = await login(client, UPLOADER)
    key = f"td277-{uuid4().hex}"
    files = [("a.pdf", PDF, "application/pdf")]

    first = await upload(client, headers, key=key, files=files, metadata='[{"title": "原标题"}]')
    assert first.status_code == 201, first.text

    retitled = await upload(
        client, headers, key=key, files=files, metadata='[{"title": "新标题"}]'
    )
    assert retitled.status_code == 409, retitled.text
    assert retitled.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    # 无 metadata（= 空元数据）与带 metadata 也算不同指纹
    omitted = await upload(client, headers, key=key, files=files)
    assert omitted.status_code == 409
    assert omitted.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    # 指纹不同 → 走的是「换内容冲突」路径：库里仍只有第一批次，没有新对象
    assert len(await rows("select * from kb_upload_batches")) == 1
    assert len(await rows("select * from kb_documents")) == 1
    assert len(object_files(env.object_dir)) == 1

    denied = await rows(
        "select * from audit_logs where action = 'school.kb.upload.denied' "
        "and metadata_json->>'reason' = 'idempotency_conflict'"
    )
    assert len(denied) == 2


async def test_idempotency_replays_when_metadata_matches(
    client: AsyncClient, env: KbEnv
) -> None:
    """TD-277 反向：同键同文件同元数据 → 仍然重放原批次（指纹加 metadata 不破坏幂等）。"""

    headers = await login(client, UPLOADER)
    key = f"td277-replay-{uuid4().hex}"
    files = [("a.pdf", PDF, "application/pdf")]
    metadata = '[{"title": "同一标题", "tags": ["规章"], "description": "同一段描述"}]'

    first = await upload(client, headers, key=key, files=files, metadata=metadata)
    assert first.status_code == 201, first.text

    second = await upload(client, headers, key=key, files=files, metadata=metadata)
    assert second.status_code == 201, second.text
    assert second.json()["replayed"] is True
    assert second.json()["batch_id"] == first.json()["batch_id"]
    assert len(await rows("select * from kb_upload_batches")) == 1
