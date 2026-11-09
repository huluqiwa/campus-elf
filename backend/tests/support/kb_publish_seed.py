"""KB-05 发布链路测试脚手架（集成与业务场景共用）。

- FakeParser/FakeEmbedder/_patch_ports：构建管线的 fake 面（与 test_kb_build_worker
  同款；CI 编排测试、本地业务场景都用它，真实引擎证据在 BS-M3-03）；
- seed_test_passed_version / seed_two_published_versions：直接落库造
  TEST_PASSED 版本（真实构建到 READY + 真实 Qdrant collection），再模拟沙箱通过；
- publisher_headers：KB-05 发布人登录。
"""

import uuid as uuid_module
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.config import get_settings
from app.kb.build_task import build_kb_version
from app.kb.parser import ParsedDocument
from tests.support.kb_env import (
    KB_PUBLISHER,
    SCHOOL_A,
    UPLOADER,
    KbEnv,
    execute,
    login,
    rows,
)

PASSWORD = "Kb-Upload-Pass-1"

MARKDOWN = "# 手册\n\n## 第一章\n\n" + "。".join(f"第{n}句有内容" for n in range(30)) + "。"


class FakeParser:
    """脚本化解析器：恒返回成功解析（按调用记次数供断言）。"""

    def __init__(self, parsed: ParsedDocument) -> None:
        self.parsed = parsed
        self.calls = 0

    async def parse(self, *, data: bytes, filename: str) -> ParsedDocument:
        self.calls += 1
        return self.parsed


class FakeEmbedder:
    """确定性 dense 向量（1024 维，按文本哈希）。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import hashlib

        if not texts:
            return []
        return [
            [
                int.from_bytes(hashlib.sha256(f"{text}|{i}".encode()).digest()[:2], "big")
                / 65535.0
                for i in range(1024)
            ]
            for text in texts
        ]


def patch_ports(monkeypatch: pytest.MonkeyPatch, parser: FakeParser) -> None:
    """把 fake ports 注入 build_task（管线编排在 CI 的唯一 fake 面）。"""

    from app.kb import build_task

    monkeypatch.setattr(build_task, "MinerUParser", lambda **kwargs: parser)
    monkeypatch.setattr(build_task, "_build_embedder", lambda settings: FakeEmbedder())
    monkeypatch.setattr(
        type(get_settings()), "kb_parser_tiers", lambda self: ["standard"], raising=True
    )


async def user_id(login_id: str) -> UUID:
    result = await rows("select id from users where login_id = :lid", lid=login_id)
    assert result, f"测试账号未种子化：{login_id}"
    return UUID(str(result[0]["id"]))


async def seed_test_passed_version(
    env: KbEnv,
    monkeypatch: pytest.MonkeyPatch,
    *,
    doc_id: UUID | None = None,
    version_no: int = 1,
    title: str = "发布用文档",
    markdown: str = MARKDOWN,
) -> tuple[UUID, UUID]:
    """直接落文档/版本（APPROVED）→ 真实构建到 READY → 置 TEST_PASSED。

    返回 (document_id, version_id)。doc_id 传入时在同一文档下追加版本（回滚目标）；
    追加前把文档拨回 APPROVED（claim 要求文档 ∈ APPROVED/FAILED），模拟
    「内容修改 → 新版本走一遍管线」的真实流转。TEST_PASSED 由本脚手架直接落库
    （模拟 KB-04A 沙箱通过的可达终态；沙箱判分链路由 KB-04A 用例覆盖）。
    markdown 可传自定义内容（KB-06 分块预览用多章节/表格/代码结构）。
    """

    patch_ports(monkeypatch, FakeParser(ParsedDocument(markdown=markdown)))
    new_doc = doc_id is None
    doc = doc_id or uuid4()
    version_id = uuid4()
    batch_id = uuid4()
    object_key = f"kb/{env.tenant_a}/{doc}/{version_id}/source.pdf"
    from app.storage.objects import build_object_store

    store = build_object_store(get_settings())
    await store.put(object_key, b"fake pdf bytes", "application/pdf")
    creator_id = str(await user_id(UPLOADER))
    if new_doc:
        await execute(
            """
            insert into kb_upload_batches (id, tenant_id, idempotency_key, request_hash,
                                           file_count, total_bytes, created_by)
            values (:id, :tenant, :key, :hash, 1, 100, :creator)
            """,
            id=str(batch_id),
            tenant=str(env.tenant_a),
            key=f"kb05-{uuid_module.uuid4()}",
            hash="c" * 64,
            creator=creator_id,
        )
        await execute(
            """
            insert into kb_documents (id, tenant_id, title, tags, status,
                                      upload_batch_id, upload_file_index, created_by)
            values (:doc, :tenant, :title, '[]'::jsonb, 'APPROVED', :batch, 0, :creator)
            """,
            doc=str(doc),
            tenant=str(env.tenant_a),
            title=title,
            batch=str(batch_id),
            creator=creator_id,
        )
    await execute(
        """
        insert into kb_document_versions (id, tenant_id, document_id, version_no,
                                          source_object_key, source_filename, source_hash,
                                          mime, size_bytes, status, scan_engine, scanned_at,
                                          created_by)
        values (:id, :tenant, :doc, :version_no, :key, '发布.pdf', :hash, 'application/pdf',
                100, 'APPROVED', 'clamav', now(), :creator)
        """,
        id=str(version_id),
        tenant=str(env.tenant_a),
        doc=str(doc),
        version_no=version_no,
        key=object_key,
        hash="d" * 64,
        creator=creator_id,
    )
    if new_doc:
        await execute(
            "update kb_documents set current_version_id = :version where id = :doc",
            version=str(version_id),
            doc=str(doc),
        )
    else:
        await execute(
            "update kb_documents set status = 'APPROVED' where id = :doc", doc=str(doc)
        )
    job_id = uuid4()
    await execute(
        """
        insert into kb_build_jobs (id, tenant_id, version_id, status, attempt, retry_count, created_by)
        values (:id, :tenant, :version, 'BUILD_QUEUED', 1, 0, :creator)
        """,
        id=str(job_id),
        tenant=str(env.tenant_a),
        version=str(version_id),
        creator=creator_id,
    )
    result = await build_kb_version({}, str(env.tenant_a), str(version_id))
    assert result["status"] == "ready", result
    await execute(
        "update kb_document_versions set status = 'TEST_PASSED' where id = :id",
        id=str(version_id),
    )
    # 文档状态只镜像 current_version（后续构建的 claim 要求文档 ∈ APPROVED/FAILED）
    await execute(
        "update kb_documents set status = 'TEST_PASSED' "
        "where id = :doc and current_version_id = :version",
        doc=str(doc),
        version=str(version_id),
    )
    return doc, version_id


async def seed_two_published_versions(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> tuple[UUID, UUID, UUID]:
    """同一文档两个版本：都构建 + TEST_PASSED，经 HTTP 发布 v1 再发布 v2。"""

    doc_id, v1 = await seed_test_passed_version(env, monkeypatch, version_no=1)
    _, v2 = await seed_test_passed_version(
        env, monkeypatch, doc_id=doc_id, version_no=2, title="发布用文档"
    )
    headers = await publisher_headers(client)
    first = await client.post(f"/api/v1/school/kb/versions/{v1}/publish", headers=headers)
    assert first.status_code == 200, first.text
    second = await client.post(f"/api/v1/school/kb/versions/{v2}/publish", headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["previous_version_id"] == str(v1)
    return doc_id, v1, v2


async def publisher_headers(
    client: AsyncClient, login_id: str = KB_PUBLISHER, school_code: str = SCHOOL_A
) -> dict[str, str]:
    """发布人登录（跨租发布人显式传 school_code=SCHOOL_B）。"""

    return await login(client, login_id, school_code)
