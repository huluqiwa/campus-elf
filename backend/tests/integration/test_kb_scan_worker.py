"""M3 KB-01A 扫描 worker 集成测试（真实 PG + 本地对象存储 + 假 clamd）。

覆盖简报 §4 红→绿清单中 worker 部分：
- 干净文件 → `PENDING_APPROVAL`，文档状态同步，写 `school.kb.scan.passed`（系统执行者）；
- 感染 → `SCAN_FAILED` + 签名，对象搬到 `quarantine/` 子路径（原件删除，留证）；
- ClamAV 不可用 + fail-closed → 保持 `UPLOADED`、累计尝试次数、不写终态审计，等看门狗补投；
- `CLAMAV_FAIL_CLOSED=false`（仅开发/测试）→ 降级放行并标记 `dev-degraded`；
- 对象哈希不符 → `SCAN_FAILED/HASH_MISMATCH`（不扫描、不放行）；
- 重复投递幂等（第二次 skipped，不重复计数/审计）；
- 跨租户不可见（not_found）；
- 看门狗补投滞留版本，并由真实 arq worker 消费完成扫描。
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from arq.worker import Worker
from httpx import AsyncClient

from app.config import get_settings
from app.kb.scan_task import retry_pending_kb_scans, scan_job_id, scan_kb_version
from app.workers.settings import build_redis_settings, create_producer_pool, queue_name
from tests.support.clamav import clamd, closed_port, reply_handler
from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import (
    B_UPLOADER,
    PDF,
    SCHOOL_B,
    UPLOADER,
    KbEnv,
    execute,
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
    async with kb_env(monkeypatch, tmp_path) as value:
        yield value


@pytest.fixture
async def client(env: KbEnv) -> AsyncIterator[AsyncClient]:
    async with make_client() as ac:
        yield ac


OK_REPLY = b"stream: OK\0"
INFECTED_REPLY = b"stream: Eicar-Test-Signature FOUND\0"


def _point_clamav(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    """把扫描客户端指向本用例的假 clamd（或无人监听的端口）。"""

    monkeypatch.setenv("CLAMAV_HOST", "127.0.0.1")
    monkeypatch.setenv("CLAMAV_PORT", str(port))
    monkeypatch.setenv("CLAMAV_TIMEOUT_SECONDS", "5")
    get_settings.cache_clear()


async def _upload_pdf(client: AsyncClient) -> tuple[str, str]:
    """上传一个 PDF，返回 (document_id, version_id)。"""

    headers = await login(client, UPLOADER)
    response = await upload(
        client,
        headers,
        key=f"scan-{uuid4().hex}",
        files=[("a.pdf", PDF, "application/pdf")],
    )
    assert response.status_code == 201, response.text
    document = response.json()["documents"][0]
    return document["id"], document["current_version"]["id"]


async def _version(version_id: str) -> Any:
    found = await rows("select * from kb_document_versions where id = :id", id=UUID(version_id))
    assert len(found) == 1
    return found[0]


async def test_clean_scan_passes_and_updates_document(
    client: AsyncClient, env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`stream: OK` → 版本/文档都到 PENDING_APPROVAL，落系统审计，对象不动。"""

    document_id, version_id = await _upload_pdf(client)
    assert await rows("select * from audit_logs where action like 'school.kb.scan.%'") == []

    async with clamd(reply_handler(OK_REPLY, expected=PDF)) as port:
        _point_clamav(monkeypatch, port)
        result = await scan_kb_version({}, str(env.tenant_a), version_id)
    assert result == {"status": "passed"}

    version = await _version(version_id)
    assert version["status"] == "PENDING_APPROVAL"
    assert version["scan_engine"] == "clamav"
    assert version["scan_attempts"] == 1
    assert version["scan_signature"] is None
    assert version["scan_error_code"] is None
    assert version["scanned_at"] is not None
    assert (env.object_dir / version["source_object_key"]).is_file()

    documents = await rows("select * from kb_documents where id = :id", id=UUID(document_id))
    assert documents[0]["status"] == "PENDING_APPROVAL"

    audits = await rows("select * from audit_logs where action = 'school.kb.scan.passed'")
    assert len(audits) == 1
    audit = audits[0]
    assert audit["actor_realm"] == "system"
    assert audit["actor_id"] is None
    assert audit["resource_id"] == version_id
    assert audit["result"] == "success"
    assert audit["metadata_json"] == {"engine": "clamav"}

    headers = await login(client, UPLOADER)
    detail = await client.get(f"/api/v1/school/kb/documents/{document_id}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "PENDING_APPROVAL"
    assert body["current_version"]["scan"]["state"] == "passed"
    assert body["current_version"]["scan"]["engine"] == "clamav"
    assert body["current_version"]["scan"]["attempts"] == 1


async def test_infected_file_fails_and_moves_to_quarantine(
    client: AsyncClient, env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`FOUND` → SCAN_FAILED + 签名；对象搬到 quarantine/ 并删除原件。"""

    document_id, version_id = await _upload_pdf(client)
    original_key = (await _version(version_id))["source_object_key"]

    async with clamd(reply_handler(INFECTED_REPLY)) as port:
        _point_clamav(monkeypatch, port)
        result = await scan_kb_version({}, str(env.tenant_a), version_id)
    assert result == {"status": "infected"}

    head, _, tail = original_key.rpartition("/")
    quarantine_key = f"{head}/quarantine/{tail}"
    version = await _version(version_id)
    assert version["status"] == "SCAN_FAILED"
    assert version["scan_signature"] == "Eicar-Test-Signature"
    assert version["scan_engine"] == "clamav"
    assert version["scan_attempts"] == 1
    assert version["scan_error_code"] is None
    assert version["source_object_key"] == quarantine_key

    assert not (env.object_dir / original_key).exists()
    quarantined = env.object_dir / quarantine_key
    assert quarantined.is_file()
    assert quarantined.read_bytes() == PDF
    assert len(object_files(env.object_dir)) == 1

    documents = await rows("select * from kb_documents where id = :id", id=UUID(document_id))
    assert documents[0]["status"] == "SCAN_FAILED"

    audits = await rows("select * from audit_logs where action = 'school.kb.scan.infected'")
    assert len(audits) == 1
    assert audits[0]["actor_realm"] == "system"
    assert audits[0]["result"] == "failure"
    assert audits[0]["metadata_json"] == {
        "engine": "clamav",
        "signature": "Eicar-Test-Signature",
    }

    headers = await login(client, UPLOADER)
    detail = await client.get(f"/api/v1/school/kb/documents/{document_id}", headers=headers)
    assert detail.json()["current_version"]["scan"]["state"] == "infected"
    assert detail.json()["current_version"]["scan"]["signature"] == "Eicar-Test-Signature"


async def test_clamav_unavailable_keeps_uploaded_for_retry(
    client: AsyncClient, env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fail-closed：扫描服务不可用不迁移状态，累计尝试次数，交看门狗补投。"""

    document_id, version_id = await _upload_pdf(client)
    _point_clamav(monkeypatch, closed_port())

    result = await scan_kb_version({}, str(env.tenant_a), version_id)
    assert result == {"status": "retry_pending"}

    version = await _version(version_id)
    assert version["status"] == "UPLOADED"
    assert version["scan_attempts"] == 1
    assert version["scan_error_code"] == "SCAN_UNAVAILABLE"
    assert version["scan_engine"] == "clamav"
    assert version["scanned_at"] is None

    documents = await rows("select * from kb_documents where id = :id", id=UUID(document_id))
    assert documents[0]["status"] == "UPLOADED"
    assert await rows("select * from audit_logs where action like 'school.kb.scan.%'") == []

    headers = await login(client, UPLOADER)
    detail = await client.get(f"/api/v1/school/kb/documents/{document_id}", headers=headers)
    scan = detail.json()["current_version"]["scan"]
    assert scan["state"] == "error"
    assert scan["error_code"] == "SCAN_UNAVAILABLE"
    assert scan["attempts"] == 1


async def test_degraded_scan_passes_when_fail_closed_disabled(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """仅开发/测试：`CLAMAV_FAIL_CLOSED=false` 时放行到 PENDING_APPROVAL 并留痕。"""

    async with (
        kb_env(monkeypatch, tmp_path, overrides={"CLAMAV_FAIL_CLOSED": "false"}) as env,
        make_client() as client,
    ):
        document_id, version_id = await _upload_pdf(client)
        _point_clamav(monkeypatch, closed_port())

        result = await scan_kb_version({}, str(env.tenant_a), version_id)
        assert result == {"status": "degraded"}

        version = await _version(version_id)
        assert version["status"] == "PENDING_APPROVAL"
        assert version["scan_engine"] == "dev-degraded"
        assert version["scan_error_code"] == "SCAN_UNAVAILABLE"
        assert version["scan_attempts"] == 1

        audits = await rows("select * from audit_logs where action = 'school.kb.scan.passed'")
        assert len(audits) == 1
        assert audits[0]["metadata_json"] == {
            "engine": "dev-degraded",
            "degraded_code": "SCAN_UNAVAILABLE",
        }

        headers = await login(client, UPLOADER)
        detail = await client.get(f"/api/v1/school/kb/documents/{document_id}", headers=headers)
        assert detail.json()["current_version"]["scan"]["state"] == "skipped"
        assert detail.json()["current_version"]["scan"]["engine"] == "dev-degraded"


async def test_hash_mismatch_fails_without_scanning(
    client: AsyncClient, env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对象内容被篡改（哈希不符）→ SCAN_FAILED/HASH_MISMATCH，且不尝试扫描。"""

    document_id, version_id = await _upload_pdf(client)
    version = await _version(version_id)
    stored = env.object_dir / version["source_object_key"]
    stored.write_bytes(PDF + b"tampered")

    # 指向无人监听的端口：若错误地先扫描，结论会是 retry_pending 而不是 hash_mismatch
    _point_clamav(monkeypatch, closed_port())
    result = await scan_kb_version({}, str(env.tenant_a), version_id)
    assert result == {"status": "hash_mismatch"}

    version = await _version(version_id)
    assert version["status"] == "SCAN_FAILED"
    assert version["scan_error_code"] == "HASH_MISMATCH"
    assert version["scan_engine"] is None
    assert version["scan_signature"] is None
    assert version["scan_attempts"] == 1

    documents = await rows("select * from kb_documents where id = :id", id=UUID(document_id))
    assert documents[0]["status"] == "SCAN_FAILED"

    audits = await rows("select * from audit_logs where action = 'school.kb.scan.failed'")
    assert len(audits) == 1
    assert audits[0]["result"] == "failure"
    assert audits[0]["metadata_json"] == {"reason": "hash_mismatch"}


async def test_second_delivery_is_skipped(
    client: AsyncClient, env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重复投递/看门狗竞态：第二次直接跳过，不重复计数、不重复审计。"""

    _, version_id = await _upload_pdf(client)

    async with clamd(reply_handler(OK_REPLY, expected=PDF)) as port:
        _point_clamav(monkeypatch, port)
        assert await scan_kb_version({}, str(env.tenant_a), version_id) == {"status": "passed"}
        assert await scan_kb_version({}, str(env.tenant_a), version_id) == {"status": "skipped"}

    version = await _version(version_id)
    assert version["scan_attempts"] == 1
    assert len(await rows("select * from audit_logs where action = 'school.kb.scan.passed'")) == 1


async def test_version_of_other_tenant_is_not_found(
    client: AsyncClient, env: KbEnv
) -> None:
    """A 校的版本在 B 校租户上下文里不可见（RLS + 仓储租户过滤）。"""

    _, version_id = await _upload_pdf(client)

    result = await scan_kb_version({}, str(env.tenant_b), version_id)
    assert result == {"status": "not_found"}
    assert (await _version(version_id))["status"] == "UPLOADED"


async def test_watchdog_reenqueues_stale_version_and_worker_scans_it(
    client: AsyncClient, env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """看门狗补投滞留 UPLOADED 版本，arq worker 消费后落到 PENDING_APPROVAL。"""

    _, version_id = await _upload_pdf(client)
    settings = get_settings()
    pool = await create_producer_pool(settings)
    try:
        # 模拟「上传时入队但任务丢失」：清掉原 job，再把版本改成 10 分钟未更新
        await pool.delete(f"arq:job:{scan_job_id(UUID(version_id), 0)}", queue_name(settings))
        assert await pool.zcard(queue_name(settings)) == 0
        await execute(
            "update kb_document_versions set updated_at = now() - interval '10 minutes' "
            "where id = :id",
            id=UUID(version_id),
        )

        async with clamd(reply_handler(OK_REPLY, expected=PDF)) as port:
            _point_clamav(monkeypatch, port)

            assert await retry_pending_kb_scans({"redis": pool}) == {"queued": 1}
            # 同 job_id 已存在：再跑一轮不重复入队
            assert await retry_pending_kb_scans({"redis": pool}) == {"queued": 0}

            worker = Worker(
                functions=[scan_kb_version],
                redis_settings=build_redis_settings(settings),
                queue_name=queue_name(settings),
                burst=True,
                poll_delay=0.05,
                max_burst_jobs=1,
                handle_signals=False,
            )
            await asyncio.wait_for(worker.async_run(), timeout=30)
    finally:
        await pool.aclose()

    version = await _version(version_id)
    assert version["status"] == "PENDING_APPROVAL"
    assert version["scan_engine"] == "clamav"
    assert version["scan_attempts"] == 1
    assert len(await rows("select * from audit_logs where action = 'school.kb.scan.passed'")) == 1


async def test_watchdog_covers_all_active_tenants(
    client: AsyncClient, env: KbEnv
) -> None:
    """看门狗按租户逐个补投：A、B 两校的滞留版本都被入队。"""

    _, version_a = await _upload_pdf(client)

    b_headers = await login(client, B_UPLOADER, SCHOOL_B)
    b_upload = await upload(
        client,
        b_headers,
        key=f"scan-b-{uuid4().hex}",
        files=[("b.pdf", PDF, "application/pdf")],
    )
    assert b_upload.status_code == 201, b_upload.text
    version_b = b_upload.json()["documents"][0]["current_version"]["id"]

    settings = get_settings()
    pool = await create_producer_pool(settings)
    try:
        # 清掉上传时入队的任务，模拟两校任务都丢失
        for version_id in (version_a, version_b):
            await pool.delete(
                f"arq:job:{scan_job_id(UUID(version_id), 0)}", queue_name(settings)
            )
        await execute(
            "update kb_document_versions set updated_at = now() - interval '10 minutes' "
            "where id in (:a, :b)",
            a=UUID(version_a),
            b=UUID(version_b),
        )

        assert await retry_pending_kb_scans({"redis": pool}) == {"queued": 2}
        assert await retry_pending_kb_scans({"redis": pool}) == {"queued": 0}
    finally:
        await pool.aclose()
