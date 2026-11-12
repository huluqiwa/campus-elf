"""业务场景 BS-M3-03b：含图 PDF 构建的图片占位符过滤（TD-310 阶段 1）——真实引擎（本地 dev 栈）。

Given 真实 MinerU（parser profile）与真实 TEI（rag profile）容器可达；
When 上传含图 PDF → 审批通过 → 创建构建（HTTP）→ worker 执行管线；
Then kb_chunks 零占位符文本（MinerU 实证形态 `![](data:image/jpeg;base64,…)` 不进块）、
     图前/图后标记文本各自保留（段界语义 D2-A）、Qdrant 点数对账一致；
     纯图 PDF（无任何正文）→ 构建显式失败 CHUNK_EMPTY（build_task.py 既有漏斗）。

第 0 步实证（2026-11-12，简报 M3-TD310-phase1-image-placeholder.md §0.2）：200×150
图块经真实 MinerU 输出 base64 占位符占整份 markdown ~99%——过滤前这些字节会进入
分词/嵌入/存储；本用例是「过滤后干净」的端到端证据。

MinerU/TEI 不可达时 skip（CI 无 GPU；本地 dev 栈常开）。PDF 夹具运行时生成
（手工 PDF + 未压缩 DeviceRGB 图块，reportlab-free；BS-M3-03 同款先例）。
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.config import get_settings
from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import (
    KB_APPROVER,
    KB_BUILDER,
    KbEnv,
    execute,
    kb_env,
    login,
    make_client,
    rows,
    upload,
)

pytestmark = [pytest.mark.business, pytest.mark.integration]

BEFORE_MARKER = "IMG-BEFORE-MARKER library hours are 08:00-22:00 on weekdays."
AFTER_MARKER = "IMG-AFTER-MARKER leave approval needs class advisor sign-off."

# 200x150 纯色图块（上半红下半蓝；无文字，避免 OCR 干扰观察占位符形态）。
_IMG_W, _IMG_H = 200, 150


def _image_pdf(*, image_only: bool) -> bytes:
    """最小手工 PDF：1 页（可选 ASCII 文本）+ 1 张未压缩 DeviceRGB 图块。"""

    def obj(num: int, body: str) -> bytes:
        return f"{num} 0 obj\n{body}\nendobj\n".encode("ascii")

    def stream_obj(num: int, dict_extra: str, payload: bytes) -> bytes:
        header = (
            f"{num} 0 obj\n<< {dict_extra} /Length {len(payload)} >>\nstream\n".encode("ascii")
        )
        return header + payload + b"\nendstream\nendobj\n"

    red = b"\xd0\x30\x30" * _IMG_W
    blue = b"\x30\x30\xd0" * _IMG_W
    image_bytes = red * (_IMG_H // 2) + blue * (_IMG_H - _IMG_H // 2)

    if image_only:
        content = b"q 200 0 0 150 150 480 cm /Im1 Do Q\n"
    else:
        before = BEFORE_MARKER.encode("ascii")
        after = AFTER_MARKER.encode("ascii")
        content = (
            b"BT /F1 16 Tf 72 720 Td (Campus Elf TD-310 Image Placeholder Scenario) Tj ET\n"
            b"BT /F1 12 Tf 72 690 Td (" + before + b") Tj ET\n"
            b"q 200 0 0 150 150 480 cm /Im1 Do Q\n"
            b"BT /F1 12 Tf 72 440 Td (" + after + b") Tj ET\n"
        )
    objects = [
        obj(1, "<< /Type /Catalog /Pages 2 0 R >>"),
        obj(2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
        obj(
            3,
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> /XObject << /Im1 6 0 R >> >> >>",
        ),
        stream_obj(4, "", content),
        obj(5, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
        stream_obj(
            6,
            f"/Type /XObject /Subtype /Image /Width {_IMG_W} /Height {_IMG_H} "
            "/ColorSpace /DeviceRGB /BitsPerComponent 8",
            image_bytes,
        ),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for item in objects:
        offsets.append(len(out))
        out += item
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


@pytest.fixture(scope="module")
def engines_available() -> Iterator[bool]:
    """真实引擎探测：MinerU/TEI 不可达即整模块 skip（CI 无 GPU/容器）。"""

    async def probe() -> tuple[bool, bool]:
        import httpx

        async def reachable(url: str) -> bool:
            try:
                async with httpx.AsyncClient(timeout=5.0) as probe_client:
                    response = await probe_client.get(url)
                return bool(response.status_code < 500)
            except Exception:
                return False

        mineru = await reachable(f"{get_settings().mineru_base_url}/v1/health")
        tei = await reachable(f"{get_settings().tei_embed_url}/health")
        return mineru, tei

    mineru, tei = asyncio.run(probe())
    if not (mineru and tei):
        pytest.skip(f"真实引擎不可达（mineru={mineru}, tei={tei}）；本用例只在本地 dev 栈跑")
    yield True


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
    migrated_database: None,
    engines_available: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[KbEnv]:
    """真实对象存储（local）+ 真实 Qdrant（test 栈）+ 真实引擎（不注入 fake）。"""

    async with kb_env(
        monkeypatch, tmp_path, overrides={"QDRANT_URL": "http://127.0.0.1:56333"}
    ) as value:
        yield value


@pytest.fixture
async def client(env: KbEnv) -> AsyncIterator[AsyncClient]:
    async with make_client() as ac:
        yield ac


async def _upload_and_approve(client: AsyncClient, pdf: bytes, run_id: str) -> str:
    """上传 PDF →（必要时直置）PENDING_APPROVAL → 审批通过，返回 version_id。

    与 BS-M3-03 同款编排：扫描 worker 不在测试进程内，等待超时后直置状态
    （扫描链路已有 BS-09 覆盖，本点聚焦构建管线）。
    """

    uploader = await login(client, "kb-uploader")
    approver = await login(client, KB_APPROVER)
    response = await upload(
        client,
        uploader,
        key=f"bs03b-{run_id}-{uuid4().hex[:12]}",
        files=[(f"image-{run_id}.pdf", pdf, "application/pdf")],
    )
    assert response.status_code == 201, response.text
    document = response.json()["documents"][0]
    version_id = document["current_version"]["id"]

    async def wait_pending() -> bool:
        for _ in range(40):
            detail = await client.get(
                f"/api/v1/school/kb/documents/{document['id']}", headers=uploader
            )
            if detail.json()["status"] == "PENDING_APPROVAL":
                return True
            await asyncio.sleep(0.5)
        return False

    if not await wait_pending():
        await execute(
            "update kb_document_versions set status = 'PENDING_APPROVAL', scan_engine = 'clamav',"
            " scanned_at = now() where id = :vid",
            vid=version_id,
        )
        await execute(
            "update kb_documents set status = 'PENDING_APPROVAL' where id = :doc",
            doc=document["id"],
        )

    submit = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/submit-approval",
        headers=uploader,
    )
    assert submit.status_code == 201, submit.text
    approve = await client.post(
        f"/api/v1/school/approvals/{submit.json()['request_id']}/approve",
        headers=approver,
        json={},
    )
    assert approve.status_code == 200, approve.text
    return str(version_id)


async def _create_build_job(
    env: KbEnv, client: AsyncClient, version_id: str
) -> str:
    builder = await login(client, KB_BUILDER)
    build = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/build", headers=builder
    )
    assert build.status_code == 201, build.text

    from app.kb.build_task import build_kb_version

    result = await build_kb_version({}, str(env.tenant_a), version_id)
    assert result["status"] == "ready", result
    return str(build.json()["job_id"])


async def test_bs_m3_03b_image_pdf_build_yields_clean_chunks(
    env: KbEnv, client: AsyncClient
) -> None:
    """含图 PDF → READY：kb_chunks 零占位符、图前/图后文本保留、Qdrant 对账。"""

    version_id = await _upload_and_approve(client, _image_pdf(image_only=False), "clean")
    job_id = await _create_build_job(env, client, version_id)

    # 快照：READY、真实档位
    builder = await login(client, KB_BUILDER)
    snapshot = await client.get(f"/api/v1/school/kb/build-jobs/{job_id}", headers=builder)
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["status"] == "READY"
    assert body["tier"] in ("standard", "basic", "flash")

    # 业务输出断言：块文本零占位符（TD-310 阶段 1 的核心验收）
    chunk_rows = await rows(
        "select chunk_type, text_markdown from kb_chunks where document_version_id = :id",
        id=version_id,
    )
    assert chunk_rows
    for row in chunk_rows:
        assert "data:image" not in row["text_markdown"]
        assert "![" not in row["text_markdown"]
        assert "images/" not in row["text_markdown"]
    joined = "\n".join(row["text_markdown"] for row in chunk_rows)
    assert "IMG-BEFORE-MARKER" in joined, "图前文本应保留"
    assert "IMG-AFTER-MARKER" in joined, "图后文本应保留"

    # Qdrant 对账（父块不入索引）
    from qdrant_client import AsyncQdrantClient

    from app.kb.indexer import collection_name, count_points

    settings = get_settings()
    collection = collection_name(env.tenant_a, UUID(version_id))
    client_q = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        indexed = await count_points(client_q, collection=collection)
    finally:
        await client_q.close()
    child_rows = [row for row in chunk_rows if row["chunk_type"] == "child"]
    assert indexed == len(child_rows)
    assert indexed >= 1

    # 清理 Qdrant collection（测试数据不残留）
    client_q = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        await client_q.delete_collection(collection)
    finally:
        await client_q.close()


async def test_bs_m3_03b_image_only_pdf_fails_with_chunk_empty(
    env: KbEnv, client: AsyncClient
) -> None:
    """纯图 PDF（无正文）→ 构建显式失败 CHUNK_EMPTY（既有漏斗承接，无新代码）。"""

    version_id = await _upload_and_approve(client, _image_pdf(image_only=True), "onlyimg")
    builder = await login(client, KB_BUILDER)
    build = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/build", headers=builder
    )
    assert build.status_code == 201, build.text

    from app.kb.build_task import build_kb_version

    result = await build_kb_version({}, str(env.tenant_a), version_id)
    assert result["status"] == "failed", result
    assert result["error_code"] == "CHUNK_EMPTY"

    job = await rows(
        "select status, error_code from kb_build_jobs where id = :id",
        id=build.json()["job_id"],
    )
    assert job[0]["status"] == "FAILED"
    assert job[0]["error_code"] == "CHUNK_EMPTY"
    version = await rows(
        "select status from kb_document_versions where id = :id", id=version_id
    )
    assert version[0]["status"] == "FAILED"
    chunk_rows = await rows(
        "select id from kb_chunks where document_version_id = :id", id=version_id
    )
    assert chunk_rows == []
