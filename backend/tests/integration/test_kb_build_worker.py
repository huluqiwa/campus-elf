"""M3 KB-03A 构建管线集成测试（CI：fake parser/embedding；真实 PG/Redis/Qdrant）。

编排全阶段（PARSING→…→READY）、降级链、失败/重试、看门狗 STALE、快照与 retry
端点的权限/跨租边界。真实 MinerU/TEI 的证据在业务场景文件（本地 dev 栈）。
"""

import os
import uuid as uuid_module
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.config import get_settings
from app.kb.build_task import build_kb_version

if TYPE_CHECKING:
    from app.kb.parser.ports import ParserError

from app.kb.parser import ParsedDocument
from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import (
    B_UPLOADER,
    KB_BUILDER,
    KB_SELF_APPROVER,
    PDF,
    SCHOOL_B,
    UPLOAD_ONLY,
    VIEWER_SCHOOL,
    KbEnv,
    execute,
    kb_env,
    login,
    make_client,
    rows,
    upload,
)

pytestmark = [pytest.mark.integration]


class FakeParser:
    """脚本化解析器：按调用序返回成功/失败（记调用次数供降级断言）。

    抛出的错误对象是 `ParserErrorStub`：它 duck-type 了 ParserError 的
    code/message，TieredParser 的 `except ParserError` 能不能接住取决于继承——
    所以它直接继承 ParserError（见下方定义顺序）。
    """

    def __init__(self, outcomes: list["ParsedDocument | ParserErrorStub"]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def parse(self, *, data: bytes, filename: str) -> ParsedDocument:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, ParserErrorStub):
            raise outcome.to_parser_error()
        return outcome


class ParserErrorStub:
    """脚本化错误（不是 ParserError 子类；FakeParser 转成真 ParserError 抛出）。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message

    def to_parser_error(self) -> "ParserError":
        from app.kb.parser.ports import ParserError as _ParserError

        return _ParserError(self.code, self.message)



class FakeEmbedder:
    """确定性 dense 向量（按文本哈希生成 4 维）。"""

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


def _patch_ports(monkeypatch: pytest.MonkeyPatch, parser: FakeParser) -> None:
    """把 fake ports 注入 build_task（管线编排在 CI 的唯一 fake 面）。"""

    from app.kb import build_task

    monkeypatch.setattr(build_task, "MinerUParser", lambda **kwargs: parser)
    monkeypatch.setattr(build_task, "_build_embedder", lambda settings: FakeEmbedder())
    monkeypatch.setattr(
        type(get_settings()), "kb_parser_tiers", lambda self: ["standard"], raising=True
    )



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
    """Qdrant 指向测试栈（56333）；Redis 前缀每次独立（kb_env 既有行为）。"""

    async with kb_env(
        monkeypatch, tmp_path, overrides={"QDRANT_URL": "http://127.0.0.1:56333"}
    ) as value:
        yield value


@pytest.fixture
async def client(env: KbEnv) -> AsyncIterator[AsyncClient]:
    async with make_client() as ac:
        yield ac


async def _user_id(login_id: str) -> UUID:
    result = await rows("select id from users where login_id = :lid", lid=login_id)
    assert result, f"测试账号未种子化：{login_id}"
    return UUID(str(result[0]["id"]))


async def _seed_approved_version(
    env: KbEnv, *, mime: str = "application/pdf", title: str = "构建用文档"
) -> tuple[UUID, UUID, UUID]:
    """直接落 APPROVED 文档/版本 + BUILD_QUEUED 任务（不经 HTTP，聚焦管线）。"""

    doc_id = uuid4()
    version_id = uuid4()
    batch_id = uuid4()
    object_key = f"kb/{env.tenant_a}/{doc_id}/{version_id}/source.pdf"
    from app.storage.objects import build_object_store

    store = build_object_store(get_settings())
    await store.put(object_key, b"fake pdf bytes", mime)
    creator = await _user_id("kb-uploader")
    await execute(
        """
        insert into kb_upload_batches (id, tenant_id, idempotency_key, request_hash,
                                       file_count, total_bytes, created_by)
        values (:id, :tenant, :key, :hash, 1, 100, :creator)
        """,
        id=str(batch_id),
        tenant=str(env.tenant_a),
        key=f"seed-{uuid_module.uuid4()}",
        hash="b" * 64,
        creator=str(creator),
    )
    # 循环引用：document（指针 NULL）→ version → 回填 current_version_id
    await execute(
        """
        insert into kb_documents (id, tenant_id, title, tags, status,
                                  upload_batch_id, upload_file_index, created_by)
        values (:doc, :tenant, :title, '[]'::jsonb, 'APPROVED', :batch, 0, :creator)
        """,
        doc=str(doc_id),
        tenant=str(env.tenant_a),
        title=title,
        batch=str(batch_id),
        creator=str(creator),
    )
    await execute(
        """
        insert into kb_document_versions (id, tenant_id, document_id, version_no,
                                          source_object_key, source_filename, source_hash,
                                          mime, size_bytes, status, scan_engine, scanned_at,
                                          created_by)
        values (:id, :tenant, :doc, 1, :key, '构建.pdf', :hash, :mime, 100, 'APPROVED',
                'clamav', now(), :creator)
        """,
        id=str(version_id),
        tenant=str(env.tenant_a),
        doc=str(doc_id),
        key=object_key,
        hash="a" * 64,
        mime=mime,
        creator=str(creator),
    )
    await execute(
        "update kb_documents set current_version_id = :version where id = :doc",
        version=str(version_id),
        doc=str(doc_id),
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
        creator=str(creator),
    )
    return job_id, doc_id, version_id


async def test_build_pipeline_reaches_ready_with_chunks_and_qdrant(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全阶段编排：APPROVED → READY；chunks/Qdrant/索引记录/审计逐项对账。"""

    markdown = "# 手册\n\n## 第一章\n\n" + "。".join(f"第{n}句有内容" for n in range(30)) + "。"
    _patch_ports(monkeypatch, FakeParser([ParsedDocument(markdown=markdown)]))
    job_id, doc_id, version_id = await _seed_approved_version(env)

    result = await build_kb_version({}, str(env.tenant_a), str(version_id))

    assert result["status"] == "ready", result
    job = await rows(
        "select status, stage, tier, progress, error_code from kb_build_jobs where id = :id",
        id=str(job_id),
    )
    assert job[0]["status"] == "READY"
    assert job[0]["stage"] == "VALIDATING"
    assert job[0]["tier"] == "standard"
    assert float(job[0]["progress"]) == 1.0
    assert job[0]["error_code"] is None

    version = await rows(
        "select status from kb_document_versions where id = :id", id=str(version_id)
    )
    assert version[0]["status"] == "READY"
    document = await rows("select status from kb_documents where id = :id", id=str(doc_id))
    assert document[0]["status"] == "READY"

    chunk_rows = await rows(
        "select chunk_type, chunk_index, embedding_status, parent_id from kb_chunks "
        "where document_version_id = :id order by chunk_index",
        id=str(version_id),
    )
    assert len(chunk_rows) >= 2  # 30 句 @默认 500 字 → 1-2 组父子块
    assert all(row["embedding_status"] == "ready" for row in chunk_rows)
    parents = [row for row in chunk_rows if row["chunk_type"] == "parent"]
    children = [row for row in chunk_rows if row["chunk_type"] == "child"]
    assert parents and children
    for row in children:
        assert row["parent_id"] is not None

    # Qdrant 对账（test 栈真实 Qdrant）
    from qdrant_client import AsyncQdrantClient

    from app.kb.indexer import collection_name, count_points, tenant_hash

    collection = collection_name(env.tenant_a, version_id)
    assert collection == f"tenant_{tenant_hash(env.tenant_a)}_kb_v{version_id}"
    settings = get_settings()
    client_q = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        indexed = await count_points(client_q, collection=collection)
        scrolled = await client_q.scroll(
            collection, limit=100, with_payload=True, with_vectors=False
        )
    finally:
        await client_q.close()
    child_chunk_rows = [row for row in chunk_rows if row["chunk_type"] == "child"]
    assert indexed == len(child_chunk_rows)  # 父块不入索引（D3）
    payload = (scrolled[0][0].payload or {}) if scrolled[0] else {}
    assert payload["tenant_id_hash"] == tenant_hash(env.tenant_a)
    assert payload["version_id"] == str(version_id)
    assert payload["is_published"] is False

    index_rows = await rows(
        "select qdrant_collection, status from kb_index_versions where document_version_id = :id",
        id=str(version_id),
    )
    assert index_rows[0]["qdrant_collection"] == collection
    assert index_rows[0]["status"] == "ready"

    audits = await rows(
        "select action from audit_logs where action = 'school.kb.build.completed' "
        "and tenant_id = :tid",
        tid=str(env.tenant_a),
    )
    assert len(audits) == 1

    client_q = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        await client_q.delete_collection(collection)
    finally:
        await client_q.close()


async def test_tier_degradation_records_fallback_reason(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """standard 失败 → 降级档成功：job.tier=实际档位 + fallback_reason 链式记录。"""

    from app.kb import build_task

    parser = FakeParser(
        [
            ParserErrorStub("MINERU_HTTP_ERROR", "standard HTTP 502"),
            ParsedDocument(markdown="# 降级文档\n\n内容足够。"),
        ]
    )
    monkeypatch.setattr(build_task, "MinerUParser", lambda **kwargs: parser)
    monkeypatch.setattr(build_task, "_build_embedder", lambda settings: FakeEmbedder())
    monkeypatch.setattr(
        type(get_settings()),
        "kb_parser_tiers",
        lambda self: ["standard", "basic"],
        raising=True,
    )
    job_id, _doc, version_id = await _seed_approved_version(env, title="降级文档")

    result = await build_kb_version({}, str(env.tenant_a), str(version_id))

    assert result["status"] == "ready", result
    job = await rows(
        "select tier, fallback_reason from kb_build_jobs where id = :id", id=str(job_id)
    )
    assert job[0]["tier"] == "basic"
    assert job[0]["fallback_reason"] == "standard: standard HTTP 502"
    assert parser.calls == 2


async def test_all_tiers_fail_marks_failed_and_retry_recovers(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """全档失败 → FAILED（版本/文档/审计）；retry 后 attempt+1 重跑成功。"""

    from app.kb import build_task

    parser = FakeParser(
        [
            ParserErrorStub("MINERU_HTTP_ERROR", "standard down"),
            ParsedDocument(markdown="# 重试成功\n\n内容。"),
        ]
    )
    monkeypatch.setattr(build_task, "MinerUParser", lambda **kwargs: parser)
    monkeypatch.setattr(build_task, "_build_embedder", lambda settings: FakeEmbedder())
    monkeypatch.setattr(
        type(get_settings()), "kb_parser_tiers", lambda self: ["standard"], raising=True
    )

    job_id, _doc, version_id = await _seed_approved_version(env, title="失败重试文档")

    first = await build_kb_version({}, str(env.tenant_a), str(version_id))
    assert first["status"] == "failed"
    assert first["error_code"] == "MINERU_HTTP_ERROR"

    failed_job = await rows(
        "select status, attempt, retry_count, error_code from kb_build_jobs where id = :id",
        id=str(job_id),
    )
    assert failed_job[0]["status"] == "FAILED"
    assert failed_job[0]["attempt"] == 1
    version = await rows(
        "select status from kb_document_versions where id = :id", id=str(version_id)
    )
    assert version[0]["status"] == "FAILED"
    chunk_count = await rows(
        "select count(*) as n from kb_chunks where document_version_id = :id", id=str(version_id)
    )
    assert chunk_count[0]["n"] == 0  # 收口原子性：失败零 chunks 残留

    headers = await login(client, KB_BUILDER)
    response = await client.post(f"/api/v1/school/kb/build-jobs/{job_id}/retry", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "BUILD_QUEUED"
    assert body["attempt"] == 2
    assert body["retry_count"] == 1

    second = await build_kb_version({}, str(env.tenant_a), str(version_id))
    assert second["status"] == "ready", second
    retried = await rows(
        "select status, attempt, retry_count from kb_build_jobs where id = :id", id=str(job_id)
    )
    assert retried[0]["status"] == "READY"
    assert retried[0]["attempt"] == 2
    assert retried[0]["retry_count"] == 1


async def test_snapshot_endpoint_fields_and_scope(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """快照（TD-288）：全字段返回；跨租 404；无 view 403。"""

    _patch_ports(monkeypatch, FakeParser([ParsedDocument(markdown="# 标题\n\n正文内容，用于切块与索引。")]))
    job_id, _doc, version_id = await _seed_approved_version(env)
    await build_kb_version({}, str(env.tenant_a), str(version_id))

    headers = await login(client, VIEWER_SCHOOL)
    response = await client.get(f"/api/v1/school/kb/build-jobs/{job_id}", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == str(job_id)
    assert body["version_id"] == str(version_id)
    assert body["status"] == "READY"
    assert body["stage"] == "VALIDATING"
    assert body["tier"] == "standard"
    assert body["attempt"] == 1
    assert body["retry_count"] == 0
    assert body["started_at"] is not None
    assert body["finished_at"] is not None

    b_headers = await login(client, B_UPLOADER, school_code=SCHOOL_B)
    cross = await client.get(f"/api/v1/school/kb/build-jobs/{job_id}", headers=b_headers)
    assert cross.status_code == 404

    upload_headers = await login(client, UPLOAD_ONLY)
    denied = await client.get(f"/api/v1/school/kb/build-jobs/{job_id}", headers=upload_headers)
    assert denied.status_code == 403


async def test_retry_endpoint_rejects_non_failed(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """READY 任务重试 → 409 KB_BUILD_NOT_RETRIABLE + 拒绝审计。"""

    _patch_ports(monkeypatch, FakeParser([ParsedDocument(markdown="# 标题\n\n正文内容，用于切块与索引。")]))
    job_id, _doc, version_id = await _seed_approved_version(env)
    await build_kb_version({}, str(env.tenant_a), str(version_id))

    headers = await login(client, KB_BUILDER)
    response = await client.post(f"/api/v1/school/kb/build-jobs/{job_id}/retry", headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "KB_BUILD_NOT_RETRIABLE"
    denied = await rows(
        "select metadata_json from audit_logs where action = 'school.kb.build.denied' "
        "and tenant_id = :tid",
        tid=str(env.tenant_a),
    )
    assert [row["metadata_json"]["code"] for row in denied] == ["KB_BUILD_NOT_RETRIABLE"]


async def test_watchdog_marks_stale_building_failed(env: KbEnv) -> None:
    """看门狗：心跳超时的 BUILDING → FAILED（STALE）+ 审计。"""

    from app.kb.build_task import retry_stale_build_jobs

    job_id, _doc, version_id = await _seed_approved_version(env)
    await execute(
        "update kb_build_jobs set status = 'BUILDING', stage = 'EMBEDDING', "
        "started_at = now(), heartbeat_at = now() - interval '1 hour' where id = :id",
        id=str(job_id),
    )
    await execute(
        "update kb_document_versions set status = 'BUILDING' where id = :id", id=str(version_id)
    )
    await execute(
        "update kb_documents set status = 'BUILDING' where current_version_id = :id",
        id=str(version_id),
    )

    result = await retry_stale_build_jobs({})

    assert result["stale"] >= 1
    job = await rows(
        "select status, error_code, message from kb_build_jobs where id = :id", id=str(job_id)
    )
    assert job[0]["status"] == "FAILED"
    assert job[0]["error_code"] == "BUILD_STALE"
    version = await rows(
        "select status from kb_document_versions where id = :id", id=str(version_id)
    )
    assert version[0]["status"] == "FAILED"
    stale_audits = await rows(
        "select metadata_json from audit_logs where action = 'school.kb.build.failed' "
        "and tenant_id = :tid and metadata_json->>'stale' = 'true'",
        tid=str(env.tenant_a),
    )
    assert len(stale_audits) >= 1


async def test_docx_pptx_build_fails_honestly(env: KbEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """TD-293：DOCX 构建在 PARSING 诚实失败（PARSER_UNAVAILABLE，可重试），版本 FAILED。"""

    from app.kb import build_task

    monkeypatch.setattr(build_task, "_build_embedder", lambda settings: FakeEmbedder())
    job_id, _doc, version_id = await _seed_approved_version(
        env,
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        title="DOCX 文档",
    )

    result = await build_kb_version({}, str(env.tenant_a), str(version_id))

    assert result["status"] == "failed"
    assert result["error_code"] == "PARSER_UNAVAILABLE"
    job = await rows("select stage, error_code from kb_build_jobs where id = :id", id=str(job_id))
    assert job[0]["stage"] == "PARSING"
    assert job[0]["error_code"] == "PARSER_UNAVAILABLE"


# ---------------------------------------------------------------------------
# KB-03B：事件持久化 + SSE + TD-291 + Minor 3（payload parent_id/只索子块/dim 校验）
# ---------------------------------------------------------------------------


async def test_build_events_persisted_in_order(env: KbEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """全阶段构建 → kb_build_events seq 连续、类型覆盖 stage×N + progress + done。"""

    markdown = "# 事件文档\n\n" + "。".join(f"内容句{n}" for n in range(30)) + "。"
    _patch_ports(monkeypatch, FakeParser([ParsedDocument(markdown=markdown)]))
    job_id, _doc, version_id = await _seed_approved_version(env, title="事件文档")

    result = await build_kb_version({}, str(env.tenant_a), str(version_id))
    assert result["status"] == "ready", result

    events = await rows(
        "select seq, event_type, payload_json from kb_build_events "
        "where job_id = :id order by seq",
        id=str(job_id),
    )
    assert len(events) >= 6
    seqs = [event["seq"] for event in events]
    assert seqs == list(range(1, len(events) + 1))
    assert events[0]["event_type"] == "build.stage"
    assert events[0]["payload_json"]["stage"] == "PARSING"
    assert events[-1]["event_type"] == "build.done"
    assert events[-1]["payload_json"]["status"] == "READY"
    types = {event["event_type"] for event in events}
    assert {"build.stage", "build.progress", "build.done"} <= types


async def test_build_failure_writes_error_event(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败路径：build.error 是最后一 seq，payload 带错误码。"""

    from app.kb import build_task

    monkeypatch.setattr(build_task, "_build_embedder", lambda settings: FakeEmbedder())
    job_id, _doc, version_id = await _seed_approved_version(
        env,
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        title="事件失败文档",
    )

    result = await build_kb_version({}, str(env.tenant_a), str(version_id))
    assert result["status"] == "failed"

    events = await rows(
        "select seq, event_type, payload_json from kb_build_events "
        "where job_id = :id order by seq",
        id=str(job_id),
    )
    assert events[-1]["event_type"] == "build.error"
    assert events[-1]["payload_json"]["error_code"] == "PARSER_UNAVAILABLE"


async def test_build_events_endpoint_replay_and_since(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """SSE：回放全量 / ?since 续传 / Last-Event-ID 头等价 / 终态后流自然关闭。"""

    markdown = "# 回放文档\n\n" + "。".join(f"第{n}句" for n in range(20)) + "。"
    _patch_ports(monkeypatch, FakeParser([ParsedDocument(markdown=markdown)]))
    job_id, _doc, version_id = await _seed_approved_version(env, title="回放文档")
    await build_kb_version({}, str(env.tenant_a), str(version_id))

    headers = await login(client, VIEWER_SCHOOL)
    request = client.build_request(
        "GET", f"/api/v1/school/kb/build-jobs/{job_id}/events", headers=headers
    )
    response = await client.send(request, stream=True)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    body = (await response.aread()).decode("utf-8")
    await response.aclose()
    # 终态 job：回放完历史即关（无心跳死循环）
    assert "event: build.stage" in body
    assert "event: build.done" in body
    assert body.count("event: build.done") == 1

    # ?since=2：只回 seq>2
    request = client.build_request(
        "GET",
        f"/api/v1/school/kb/build-jobs/{job_id}/events",
        headers=headers,
        params={"since": 2},
    )
    response = await client.send(request, stream=True)
    body = (await response.aread()).decode("utf-8")
    await response.aclose()
    assert "event: build.done" in body
    # since=2 后不应有 seq=1（seq:10/11 等含 1 的子串不算——用行首精确匹配）
    assert 'data: {"stage":"PARSING"' not in body  # seq=1 的 stage 事件不应出现

    # Last-Event-ID 头等价
    request = client.build_request(
        "GET",
        f"/api/v1/school/kb/build-jobs/{job_id}/events",
        headers={**headers, "Last-Event-ID": "2"},
    )
    response = await client.send(request, stream=True)
    body = (await response.aread()).decode("utf-8")
    await response.aclose()
    assert 'data: {"stage":"PARSING"' not in body


async def test_build_events_endpoint_permission(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """SSE 权限：跨租 404、无 view 403。"""

    _patch_ports(monkeypatch, FakeParser([ParsedDocument(markdown="# 标题\n\n正文。")]))
    job_id, _doc, version_id = await _seed_approved_version(env)
    await build_kb_version({}, str(env.tenant_a), str(version_id))

    b_headers = await login(client, B_UPLOADER, school_code=SCHOOL_B)
    cross = await client.get(f"/api/v1/school/kb/build-jobs/{job_id}/events", headers=b_headers)
    assert cross.status_code == 404

    upload_headers = await login(client, UPLOAD_ONLY)
    denied = await client.get(f"/api/v1/school/kb/build-jobs/{job_id}/events", headers=upload_headers)
    assert denied.status_code == 403


async def test_qdrant_payload_parent_id_and_child_only_count(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Minor 3：payload parent_id 非空指向父块；点数 == 子块数（父块不入索引）。"""

    markdown = "# 父子测试\n\n" + "。".join(f"第{n}句有内容" for n in range(30)) + "。"
    _patch_ports(monkeypatch, FakeParser([ParsedDocument(markdown=markdown)]))
    job_id, _doc, version_id = await _seed_approved_version(env, title="父子测试")
    await build_kb_version({}, str(env.tenant_a), str(version_id))
    assert (await rows("select status from kb_build_jobs where id = :id", id=str(job_id)))[0][
        "status"
    ] == "READY"

    chunk_rows = await rows(
        "select id, chunk_type from kb_chunks where document_version_id = :id order by chunk_index",
        id=str(version_id),
    )
    child_rows = [row for row in chunk_rows if row["chunk_type"] == "child"]
    parent_ids = {str(row["id"]) for row in chunk_rows if row["chunk_type"] == "parent"}

    from qdrant_client import AsyncQdrantClient

    from app.kb.indexer import collection_name, count_points

    settings = get_settings()
    collection = collection_name(env.tenant_a, version_id)
    client_q = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        indexed = await count_points(client_q, collection=collection)
        assert indexed == len(child_rows)  # 父块不入索引
        scrolled = await client_q.scroll(
            collection, limit=100, with_payload=True, with_vectors=False
        )
    finally:
        await client_q.close()
    points = scrolled[0]
    assert len(points) == len(child_rows)
    child_chunk_ids = {str(row["id"]) for row in child_rows}
    for point in points:
        payload = point.payload or {}
        assert payload["chunk_type"] == "child"
        assert payload["parent_id"] in parent_ids  # 指向真实父块行
        assert str(point.id) in child_chunk_ids  # 点 id == 子块 chunk 行 id
    client_q = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        await client_q.delete_collection(collection)
    finally:
        await client_q.close()


async def test_embed_dim_drift_fails_build(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4：实际维度与 KB_EMBED_DIM 漂移 → FAILED EMBED_DIM_MISMATCH。"""

    class WrongDimEmbedder:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            import hashlib

            return [
                [float(int.from_bytes(hashlib.sha256(text.encode()).digest()[:1])) % 7 + 1]
                for text in texts
            ]  # 1 维 ≠ 配置 1024

    from app.kb import build_task

    _patch_ports(monkeypatch, FakeParser([ParsedDocument(markdown="# 维度\n\n正文。")]))
    monkeypatch.setattr(build_task, "_build_embedder", lambda settings: WrongDimEmbedder())
    _job_id, _doc, version_id = await _seed_approved_version(env, title="维度漂移文档")

    result = await build_kb_version({}, str(env.tenant_a), str(version_id))

    assert result["status"] == "failed"
    assert result["error_code"] == "EMBED_DIM_MISMATCH"


async def test_version_approvals_endpoint_tdj291(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """TD-291：REJECTED 版本查审批单 → 理由/审批人可见；空版本返回空表；跨租 404。"""

    uploader = await login(client, "kb-uploader")
    approver = await login(client, KB_SELF_APPROVER)
    headers_b = await login(client, B_UPLOADER, school_code=SCHOOL_B)

    response = await upload(
        client,
        uploader,
        key=f"td291-{uuid_module.uuid4().hex[:12]}",
        files=[("td291.pdf", PDF, "application/pdf")],
    )
    assert response.status_code == 201
    doc = response.json()["documents"][0]
    version_id = doc["current_version"]["id"]

    await execute(
        "update kb_document_versions set status = 'PENDING_APPROVAL', scan_engine='clamav',"
        " scanned_at=now() where id = :vid",
        vid=version_id,
    )
    await execute(
        "update kb_documents set status = 'PENDING_APPROVAL' where id = :doc", doc=doc["id"]
    )

    submit = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/submit-approval",
        headers=uploader,
    )
    assert submit.status_code == 201
    request_id = submit.json()["request_id"]
    reject = await client.post(
        f"/api/v1/school/approvals/{request_id}/reject",
        headers=approver,
        json={"reason": "内容与学校口径不符"},
    )
    assert reject.status_code == 200

    listing = await client.get(
        f"/api/v1/school/kb/document-versions/{version_id}/approvals", headers=uploader
    )
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "REJECTED"
    assert items[0]["reject_reason"] == "内容与学校口径不符"
    assert items[0]["result"] == "reject"

    response2 = await upload(
        client,
        uploader,
        key=f"td291b-{uuid_module.uuid4().hex[:12]}",
        files=[("td291b.pdf", PDF, "application/pdf")],
    )
    version2 = response2.json()["documents"][0]["current_version"]["id"]
    await execute(
        "update kb_document_versions set status='PENDING_APPROVAL', scan_engine='clamav',"
        " scanned_at=now() where id = :vid",
        vid=version2,
    )
    await execute(
        "update kb_documents set status='PENDING_APPROVAL' where id = :doc",
        doc=response2.json()["documents"][0]["id"],
    )
    empty = await client.get(
        f"/api/v1/school/kb/document-versions/{version2}/approvals", headers=uploader
    )
    assert empty.status_code == 200
    assert empty.json()["items"] == []

    cross = await client.get(
        f"/api/v1/school/kb/document-versions/{version_id}/approvals", headers=headers_b
    )
    assert cross.status_code == 404
