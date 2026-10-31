"""业务场景 BS-09：知识库上传 → 异步扫描 → 待审批（M3 KB-01A）。

Given 教师（只有 upload、看不到列表）上传合法 PDF/MD，反派场景含感染文件与
      伪装成 docx 的 ZIP 炸弹；
When 上传接口整批落库入队、扫描 worker（假 clamd）逐个走完；
Then 合法文件 UPLOADED → PENDING_APPROVAL 且可提交审批（扫描前不可提交）；
     感染文件 SCAN_FAILED、对象隔离、不可提交；
     超 15 个 / 51MB / ZIP 炸弹整批 422 且库与存储零残留；
     跨租 404（含 worker 不可见）、无权限 403；
     审计（上传 / 扫描通过 / 感染 / 拒绝）逐条可查，且本点不产生解析/构建动作。
"""

import io
import json
import os
import zipfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.config import get_settings
from app.kb.scan_task import scan_kb_version
from app.kb.status import NotSubmittableError, assert_submittable
from tests.support.clamav import clamd, reply_handler
from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import (
    B_UPLOADER,
    MD,
    NO_PERM,
    PDF,
    SCHOOL_B,
    UPLOAD_ONLY,
    VIEWER_SCHOOL,
    KbEnv,
    kb_env,
    login,
    make_client,
    object_files,
    rows,
    upload,
)

pytestmark = [pytest.mark.business, pytest.mark.integration]

OK_REPLY = b"stream: OK\0"
INFECTED_REPLY = b"stream: Eicar-Test-Signature FOUND\0"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


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
    async with kb_env(monkeypatch, tmp_path) as value:
        yield value


@pytest.fixture
async def client(env: KbEnv) -> AsyncIterator[AsyncClient]:
    async with make_client() as ac:
        yield ac


async def _scan_versions(
    env: KbEnv,
    monkeypatch: pytest.MonkeyPatch,
    version_ids: list[str],
    reply: bytes,
) -> list[dict[str, str]]:
    """起假 clamd，逐个版本直调 worker（模拟 arq 消费）。"""

    async with clamd(reply_handler(reply)) as port:
        monkeypatch.setenv("CLAMAV_HOST", "127.0.0.1")
        monkeypatch.setenv("CLAMAV_PORT", str(port))
        monkeypatch.setenv("CLAMAV_TIMEOUT_SECONDS", "5")
        get_settings.cache_clear()
        return [
            await scan_kb_version({}, str(env.tenant_a), version_id)
            for version_id in version_ids
        ]


async def _version_status(version_id: str) -> str:
    found = await rows("select status from kb_document_versions where id = :id", id=UUID(version_id))
    assert len(found) == 1
    return str(found[0]["status"])


def _zip_bomb_docx() -> bytes:
    """结构合法但压缩比异常的 docx（1MB 零字节压成 ~1KB）。"""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", b"0" * (1024 * 1024))
    return buffer.getvalue()


async def test_bs_m3_01_upload_scan_then_pending_approval(
    client: AsyncClient, env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """教师上传合法文件：扫描前不可提交 → 扫描通过后可提交，校管可见。"""

    teacher = await login(client, UPLOAD_ONLY)
    metadata = json.dumps(
        [{"title": "校园安全手册", "tags": ["安全"]}, {}], ensure_ascii=False
    )
    response = await upload(
        client,
        teacher,
        key=f"bs09-{uuid4().hex}",
        files=[
            ("校园安全.pdf", PDF, "application/pdf"),
            ("校规.md", MD, "text/markdown"),
        ],
        metadata=metadata,
    )
    assert response.status_code == 201, response.text
    documents = response.json()["documents"]
    assert [doc["title"] for doc in documents] == ["校园安全手册", "校规"]
    assert [doc["status"] for doc in documents] == ["UPLOADED", "UPLOADED"]
    assert [doc["current_version"]["scan"]["state"] for doc in documents] == [
        "pending",
        "pending",
    ]
    document_ids = [doc["id"] for doc in documents]
    version_ids = [doc["current_version"]["id"] for doc in documents]

    # 扫描前：不可提交审批（KB-02 复用守卫），审计只有「已上传」
    for version_id in version_ids:
        with pytest.raises(NotSubmittableError):
            assert_submittable(await _version_status(version_id))
    upload_audits = await rows(
        "select * from audit_logs where action = 'school.kb.documents.uploaded'"
    )
    assert len(upload_audits) == 1
    assert upload_audits[0]["metadata_json"]["file_count"] == 2
    assert upload_audits[0]["metadata_json"]["extensions"] == [".md", ".pdf"]
    assert upload_audits[0]["request_id"]
    assert await rows("select * from audit_logs where action like 'school.kb.scan.%'") == []

    # 对象按租户前缀落盘
    assert len(object_files(env.object_dir)) == 2
    keys = [
        row["source_object_key"]
        for row in await rows("select source_object_key from kb_document_versions")
    ]
    assert all(str(env.tenant_a) in key and key.startswith("kb/") for key in keys)

    # worker 扫描通过
    results = await _scan_versions(env, monkeypatch, version_ids, OK_REPLY)
    assert results == [{"status": "passed"}, {"status": "passed"}]

    for version_id in version_ids:
        status = await _version_status(version_id)
        assert status == "PENDING_APPROVAL"
        assert_submittable(status)
    document_rows = await rows("select id, status from kb_documents")
    assert {str(row["id"]) for row in document_rows} == set(document_ids)
    assert {row["status"] for row in document_rows} == {"PENDING_APPROVAL"}

    passed = await rows(
        "select * from audit_logs where action = 'school.kb.scan.passed' order by created_at"
    )
    assert len(passed) == 2
    assert {row["resource_id"] for row in passed} == set(version_ids)
    for audit in passed:
        assert audit["actor_realm"] == "system"
        assert audit["actor_id"] is None
        assert audit["result"] == "success"
        assert audit["metadata_json"] == {"engine": "clamav"}

    # 本点只做上传 + 扫描：审计动作集合闭合，没有解析/构建痕迹
    actions = {
        row["action"]
        for row in await rows("select action from audit_logs where action like 'school.kb.%'")
    }
    assert actions == {"school.kb.documents.uploaded", "school.kb.scan.passed"}

    # 校管（school 范围 + view）看到 passed；教师只有 upload，看不到列表
    admin = await login(client, VIEWER_SCHOOL)
    listing = await client.get("/api/v1/school/kb/documents", headers=admin)
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert {item["id"] for item in items} == set(document_ids)
    assert all(item["current_version"]["scan"]["state"] == "passed" for item in items)
    assert (await client.get("/api/v1/school/kb/documents", headers=teacher)).status_code == 403


async def test_bs_m3_01_infected_upload_is_quarantined_and_not_submittable(
    client: AsyncClient, env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """感染文件：SCAN_FAILED + 隔离区留证，不可提交审批。"""

    teacher = await login(client, UPLOAD_ONLY)
    response = await upload(
        client,
        teacher,
        key=f"bs09-inf-{uuid4().hex}",
        files=[("eicar.pdf", PDF, "application/pdf")],
    )
    assert response.status_code == 201, response.text
    version_id = response.json()["documents"][0]["current_version"]["id"]
    original_key = (
        await rows("select source_object_key from kb_document_versions where id = :id", id=UUID(version_id))
    )[0]["source_object_key"]

    assert await _scan_versions(env, monkeypatch, [version_id], INFECTED_REPLY) == [
        {"status": "infected"}
    ]

    version = (
        await rows("select * from kb_document_versions where id = :id", id=UUID(version_id))
    )[0]
    assert version["status"] == "SCAN_FAILED"
    assert version["scan_signature"] == "Eicar-Test-Signature"
    assert version["scan_engine"] == "clamav"
    with pytest.raises(NotSubmittableError):
        assert_submittable(version["status"])

    # 原件已从原路径删除，隔离副本保留同样内容
    head, _, tail = original_key.rpartition("/")
    quarantine_key = f"{head}/quarantine/{tail}"
    assert version["source_object_key"] == quarantine_key
    assert not (env.object_dir / original_key).exists()
    quarantined = env.object_dir / quarantine_key
    assert quarantined.is_file() and quarantined.read_bytes() == PDF
    assert len(object_files(env.object_dir)) == 1

    infected = await rows("select * from audit_logs where action = 'school.kb.scan.infected'")
    assert len(infected) == 1
    assert infected[0]["actor_realm"] == "system"
    assert infected[0]["result"] == "failure"
    assert infected[0]["metadata_json"] == {
        "engine": "clamav",
        "signature": "Eicar-Test-Signature",
    }
    assert await rows("select * from audit_logs where action = 'school.kb.scan.passed'") == []

    admin = await login(client, VIEWER_SCHOOL)
    detail = await client.get(
        f"/api/v1/school/kb/documents/{response.json()['documents'][0]['id']}", headers=admin
    )
    assert detail.json()["status"] == "SCAN_FAILED"
    assert detail.json()["current_version"]["scan"]["state"] == "infected"


async def test_bs_m3_01_batch_limits_reject_everything_without_residue(
    client: AsyncClient, env: KbEnv
) -> None:
    """超 15 个 / 51MB / ZIP 炸弹：整批 422，库与存储零残留，拒绝审计留痕。"""

    teacher = await login(client, UPLOAD_ONLY)

    too_many = await upload(
        client,
        teacher,
        key=f"bs09-16-{uuid4().hex}",
        files=[(f"f{index}.pdf", PDF, "application/pdf") for index in range(16)],
    )
    assert too_many.status_code == 422, too_many.text
    assert too_many.json()["detail"]["code"] == "TOO_MANY_FILES"

    oversized_pdf = b"%PDF-1.7\n" + b"0" * (51 * 1024 * 1024)
    too_big = await upload(
        client,
        teacher,
        key=f"bs09-big-{uuid4().hex}",
        files=[("big.pdf", oversized_pdf, "application/pdf")],
    )
    assert too_big.status_code == 422, too_big.text
    assert too_big.json()["detail"]["code"] == "BATCH_VALIDATION_FAILED"
    assert too_big.json()["detail"]["files"][0]["code"] == "FILE_TOO_LARGE"

    bomb = await upload(
        client,
        teacher,
        key=f"bs09-bomb-{uuid4().hex}",
        files=[("bomb.docx", _zip_bomb_docx(), DOCX_MIME)],
    )
    assert bomb.status_code == 422, bomb.text
    assert bomb.json()["detail"]["files"][0]["code"] == "ZIP_RATIO_TOO_HIGH"

    assert await rows("select * from kb_upload_batches") == []
    assert await rows("select * from kb_documents") == []
    assert await rows("select * from kb_document_versions") == []
    assert object_files(env.object_dir) == []

    denied = await rows(
        "select * from audit_logs where action = 'school.kb.upload.denied' order by created_at"
    )
    assert len(denied) == 3
    assert {row["metadata_json"]["reason"] for row in denied} == {
        "too_many_files",
        "batch_validation_failed",
    }
    assert all(row["result"] == "denied" for row in denied)
    codes = [row["metadata_json"]["codes"] for row in denied]
    assert ["TOO_MANY_FILES"] in codes
    assert ["FILE_TOO_LARGE"] in codes
    assert ["ZIP_RATIO_TOO_HIGH"] in codes
    assert all(row["metadata_json"]["file_count"] > 0 for row in denied)


async def test_bs_m3_01_permission_and_tenant_boundaries(
    client: AsyncClient, env: KbEnv
) -> None:
    """无权限 403；B 校文档在 A 校列表/详情不可见，扫描 worker 也读不到。"""

    no_perm = await login(client, NO_PERM)
    denied = await upload(
        client, no_perm, key=f"bs09-noperm-{uuid4().hex}",
        files=[("a.pdf", PDF, "application/pdf")],
    )
    assert denied.status_code == 403
    permission_denied = await rows(
        "select * from audit_logs where action = 'auth.permission.denied' and result = 'failure'"
    )
    assert permission_denied != []

    b_headers = await login(client, B_UPLOADER, SCHOOL_B)
    b_upload = await upload(
        client, b_headers, key=f"bs09-b-{uuid4().hex}",
        files=[("b.pdf", PDF, "application/pdf")],
    )
    assert b_upload.status_code == 201, b_upload.text
    b_document = b_upload.json()["documents"][0]

    a_admin = await login(client, VIEWER_SCHOOL)
    assert (
        await client.get(f"/api/v1/school/kb/documents/{b_document['id']}", headers=a_admin)
    ).status_code == 404
    assert (await client.get("/api/v1/school/kb/documents", headers=a_admin)).json()["items"] == []

    # 扫描任务必须带租户上下文：A 校上下文扫 B 校版本 → not_found，B 校状态不动
    result = await scan_kb_version({}, str(env.tenant_a), b_document["current_version"]["id"])
    assert result == {"status": "not_found"}
    assert await _version_status(b_document["current_version"]["id"]) == "UPLOADED"
