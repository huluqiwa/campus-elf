"""M3 KB-02 审批与构建门禁集成测试（真实 PG；扫描用 dev-skip 直达 PENDING_APPROVAL）。

覆盖简报 §4 红→绿清单：
- 提交：开单 + 版本快照 + 审计；重复提交 409；UPLOADED 409 `KB_NOT_SUBMITTABLE`；
- 通过/驳回：版本与文档状态同事务双写、审计、待办/已办；驳回理由必填；REJECTED 不可再提交；
- 自批禁令：提交人/上传者两向 403（`sod.py` 判定内核，无例外通道）；
- 撤销：提交人撤销 → CANCELLED 且版本回 PENDING_APPROVAL 可再提交；非提交人 403；
- 构建门禁：未审批 409 + 零任务行 + 审计；APPROVED → BUILD_QUEUED；并发/二次创建唯一赢家；
- 元数据：免审但审计前后值，版本状态不变；
- 跨租：404（不泄露存在性）。
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient, Response

from app.kb.service import KbError, decide_version_approval
from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import (
    B_BUILDER,
    B_UPLOADER,
    KB_APPROVER,
    KB_BUILDER,
    KB_SELF_APPROVER,
    MD,
    SCHOOL_B,
    UPLOAD_ONLY,
    UPLOADER,
    KbEnv,
    execute,
    kb_env,
    login,
    make_client,
    rows,
    upload,
)

pytestmark = [pytest.mark.integration]

PASSWORD = "Kb-Upload-Pass-1"


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
    """dev-skip 打开：上传直达 PENDING_APPROVAL（审批链路的可达前提）。"""

    async with kb_env(monkeypatch, tmp_path, overrides={"DEV_SKIP_FILE_SCAN": "true"}) as value:
        yield value


@pytest.fixture
async def client(env: KbEnv) -> AsyncIterator[AsyncClient]:
    async with make_client() as ac:
        yield ac


async def _uploader_headers(client: AsyncClient) -> dict[str, str]:
    return await login(client, UPLOADER)


async def _upload_one(
    client: AsyncClient, headers: dict[str, str], *, key: str | None = None
) -> tuple[str, str, str]:
    """上传一个 md 文件（dev-skip → PENDING_APPROVAL），返回 (document_id, version_id, title)。"""

    idempotency = key or f"kb02-{uuid4().hex[:16]}"
    response = await upload(
        client,
        headers,
        key=idempotency,
        files=[("kb02.md", MD, "text/markdown")],
        metadata='[{"title": "审批用文档", "tags": ["规章"]}]',
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["replayed"] is False
    document = payload["documents"][0]
    assert document["status"] == "PENDING_APPROVAL"
    version = document["current_version"]
    assert version is not None
    return document["id"], version["id"], document["title"]


async def _submit(
    client: AsyncClient, headers: dict[str, str], version_id: str
) -> Response:
    return await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/submit-approval",
        headers=headers,
    )


async def _decide(
    client: AsyncClient, headers: dict[str, str], request_id: str, action: str
) -> Response:
    return await client.post(
        f"/api/v1/school/approvals/{request_id}/{action}",
        headers=headers,
        json={},
    )


async def _audit_rows(action: str, tenant_id: UUID) -> list[dict[str, Any]]:
    return await rows(
        "select action, actor_id, result, metadata_json from audit_logs "
        "where action = :action and tenant_id = :tid order by created_at",
        action=action,
        tid=tenant_id,
    )


async def _user_id(login_id: str) -> UUID:
    result = await rows("select id from users where login_id = :lid", lid=login_id)
    assert result, f"测试账号未种子化：{login_id}"
    return UUID(str(result[0]["id"]))


async def test_submit_creates_pending_request_with_snapshot_and_audit(
    client: AsyncClient, env: KbEnv
) -> None:
    """Given PENDING_APPROVAL 版本；When 提交审批；Then 开单 + 快照 + 步骤 + submit 留痕 + 审计。"""

    headers = await _uploader_headers(client)
    _, version_id, title = await _upload_one(client, headers)

    response = await _submit(client, headers, version_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["document_version_id"] == version_id
    assert title in body["title"]

    request_rows = await rows(
        "select status, submitter_id, payload_json, current_step "
        "from approval_requests where document_version_id = :vid",
        vid=version_id,
    )
    assert len(request_rows) == 1
    row = request_rows[0]
    assert row["status"] == "PENDING"
    assert row["payload_json"]["document_title"] == title
    assert row["payload_json"]["version_id"] == version_id
    assert row["payload_json"]["tags"] == ["规章"]
    assert row["payload_json"]["scan_engine"] == "dev-skip"
    steps = await rows(
        "select step_no, status from approval_steps where request_id = :rid",
        rid=(await rows("select id from approval_requests where document_version_id = :vid", vid=version_id))[0]["id"],
    )
    assert [(s["step_no"], s["status"]) for s in steps] == [(1, "PENDING")]
    submitted_audit = await _audit_rows("school.kb.approval.submitted", env.tenant_a)
    assert len(submitted_audit) == 1
    assert submitted_audit[0]["result"] == "success"

    # 待办里能看到
    approver_headers = await login(client, KB_APPROVER)
    todo = await client.get("/api/v1/school/approvals/todo", headers=approver_headers)
    assert todo.status_code == 200
    assert any(item["id"] == body["request_id"] for item in todo.json()["items"])


async def test_submit_twice_conflicts(client: AsyncClient, env: KbEnv) -> None:
    """同一版本第二笔提交 → 409 APPROVAL_ALREADY_PENDING（部分唯一索引兜底前先有显式判据）。"""

    headers = await _uploader_headers(client)
    _, version_id, _ = await _upload_one(client, headers)
    assert (await _submit(client, headers, version_id)).status_code == 201
    second = await _submit(client, headers, version_id)
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "APPROVAL_ALREADY_PENDING"


async def test_submit_rejected_for_uploaded_version(client: AsyncClient, env: KbEnv) -> None:
    """Given UPLOADED（扫描未过）；When 提交；Then 409 KB_NOT_SUBMITTABLE + 拒绝审计 + 零审批单。"""

    headers = await _uploader_headers(client)
    _, version_id, _ = await _upload_one(client, headers)
    await execute(
        "update kb_document_versions set status = 'UPLOADED', scan_engine = null, "
        "scanned_at = null where id = :vid",
        vid=version_id,
    )
    response = await _submit(client, headers, version_id)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "KB_NOT_SUBMITTABLE"
    requests_count = await rows("select count(*) as n from approval_requests")
    assert requests_count[0]["n"] == 0
    denied = await _audit_rows("school.kb.approval.denied", env.tenant_a)
    assert [row["metadata_json"]["code"] for row in denied] == ["KB_NOT_SUBMITTABLE"]


async def test_approve_moves_version_and_document_to_approved(
    client: AsyncClient, env: KbEnv
) -> None:
    """Given 待审批单；When 审批人通过；Then 版本与文档 APPROVED、待办清空、已办可见、审计落库。"""

    headers = await _uploader_headers(client)
    _, version_id, _ = await _upload_one(client, headers)
    submit = await _submit(client, headers, version_id)
    request_id = submit.json()["request_id"]

    approver_headers = await login(client, KB_APPROVER)
    approve = await _decide(client, approver_headers, request_id, "approve")
    assert approve.status_code == 200, approve.text
    assert approve.json()["status"] == "APPROVED"

    version_rows = await rows(
        "select status from kb_document_versions where id = :vid", vid=version_id
    )
    assert version_rows[0]["status"] == "APPROVED"
    document_rows = await rows(
        "select status from kb_documents where current_version_id = :vid", vid=version_id
    )
    assert document_rows[0]["status"] == "APPROVED"

    request_rows = await rows(
        "select status, result, completed_at, reject_reason from approval_requests where id = :rid",
        rid=request_id,
    )
    assert request_rows[0]["status"] == "APPROVED"
    assert request_rows[0]["result"] == "approve"
    assert request_rows[0]["completed_at"] is not None
    assert request_rows[0]["reject_reason"] is None

    todo = await client.get("/api/v1/school/approvals/todo", headers=approver_headers)
    assert todo.status_code == 200
    assert todo.json()["items"] == []
    done = await client.get("/api/v1/school/approvals/done", headers=approver_headers)
    assert done.status_code == 200
    assert any(item["id"] == request_id for item in done.json()["items"])

    approved_audit = await _audit_rows("school.kb.approval.approved", env.tenant_a)
    assert len(approved_audit) == 1
    assert approved_audit[0]["metadata_json"]["to_status"] == "APPROVED"


async def test_self_approval_by_submitter_forbidden(client: AsyncClient, env: KbEnv) -> None:
    """Given 提交人同持审批码；When 审批自己提交的单；Then 403 且版本/审批单原状。"""

    headers = await login(client, KB_SELF_APPROVER)
    _, version_id, _ = await _upload_one(client, headers)
    request_id = (await _submit(client, headers, version_id)).json()["request_id"]

    approve = await _decide(client, headers, request_id, "approve")
    assert approve.status_code == 403
    assert approve.json()["detail"]["code"] == "SELF_APPROVAL_FORBIDDEN"

    version_rows = await rows(
        "select status from kb_document_versions where id = :vid", vid=version_id
    )
    assert version_rows[0]["status"] == "PENDING_APPROVAL"
    request_rows = await rows(
        "select status from approval_requests where id = :rid", rid=request_id
    )
    assert request_rows[0]["status"] == "PENDING"
    denied = await _audit_rows("school.kb.approval.denied", env.tenant_a)
    assert [row["metadata_json"]["code"] for row in denied] == ["SELF_APPROVAL_FORBIDDEN"]


async def test_approver_cannot_approve_document_uploaded_by_self(
    client: AsyncClient, env: KbEnv
) -> None:
    """上传者不得自批（目录口径）：自批账号上传、他人提交，自批账号审批 → 403。"""

    self_approver_headers = await login(client, KB_SELF_APPROVER)
    _, version_id, _ = await _upload_one(client, self_approver_headers)
    # UPLOADER（self 范围）看不到别人的文档 → 404（数据范围 fail-closed 是另一条判据），
    # 所以用 UPLOAD_ONLY（school 范围 + upload）提交他人上传的版本。
    upload_only_headers = await login(client, UPLOAD_ONLY)
    submit = await _submit(client, upload_only_headers, version_id)
    assert submit.status_code == 201, submit.text
    request_id = submit.json()["request_id"]

    approve = await _decide(client, self_approver_headers, request_id, "approve")
    assert approve.status_code == 403
    assert approve.json()["detail"]["code"] == "SELF_APPROVAL_FORBIDDEN"


async def test_reject_requires_reason_and_moves_to_rejected(
    client: AsyncClient, env: KbEnv
) -> None:
    """Given 待审批单；When 无理由驳回 → 422；有理由驳回 → REJECTED + 理由留痕；再提交不可。"""

    headers = await _uploader_headers(client)
    _, version_id, _ = await _upload_one(client, headers)
    request_id = (await _submit(client, headers, version_id)).json()["request_id"]

    approver_headers = await login(client, KB_APPROVER)
    empty = await client.post(
        f"/api/v1/school/approvals/{request_id}/reject",
        headers=approver_headers,
        json={},
    )
    assert empty.status_code == 422
    assert empty.json()["detail"]["code"] == "REJECT_REASON_REQUIRED"

    reject = await client.post(
        f"/api/v1/school/approvals/{request_id}/reject",
        headers=approver_headers,
        json={"reason": "文件与目录主题不符，请重新整理"},
    )
    assert reject.status_code == 200, reject.text

    version_rows = await rows(
        "select status from kb_document_versions where id = :vid", vid=version_id
    )
    assert version_rows[0]["status"] == "REJECTED"
    request_rows = await rows(
        "select status, result, reject_reason from approval_requests where id = :rid",
        rid=request_id,
    )
    assert request_rows[0]["status"] == "REJECTED"
    assert request_rows[0]["result"] == "reject"
    assert request_rows[0]["reject_reason"] == "文件与目录主题不符，请重新整理"

    detail = await client.get(
        f"/api/v1/school/approvals/{request_id}", headers=approver_headers
    )
    assert detail.status_code == 200
    assert detail.json()["request"]["reject_reason"] == "文件与目录主题不符，请重新整理"
    assert any(
        action["action"] == "reject" and action["reason"] == "文件与目录主题不符，请重新整理"
        for action in detail.json()["actions"]
    )

    resubmit = await _submit(client, headers, version_id)
    assert resubmit.status_code == 409
    assert resubmit.json()["detail"]["code"] == "KB_NOT_SUBMITTABLE"


async def test_cancel_by_submitter_allows_resubmit(client: AsyncClient, env: KbEnv) -> None:
    """提交人撤销 → CANCELLED、版本保持 PENDING_APPROVAL、可再提交；非提交人撤销 403。"""

    headers = await _uploader_headers(client)
    _, version_id, _ = await _upload_one(client, headers)
    request_id = (await _submit(client, headers, version_id)).json()["request_id"]

    upload_only_headers = await login(client, UPLOAD_ONLY)
    forbidden = await client.post(
        f"/api/v1/school/approvals/{request_id}/cancel", headers=upload_only_headers
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"]["code"] == "APPROVAL_CANCEL_FORBIDDEN"

    cancel = await client.post(
        f"/api/v1/school/approvals/{request_id}/cancel", headers=headers
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "CANCELLED"

    version_rows = await rows(
        "select status from kb_document_versions where id = :vid", vid=version_id
    )
    assert version_rows[0]["status"] == "PENDING_APPROVAL"

    resubmit = await _submit(client, headers, version_id)
    assert resubmit.status_code == 201
    assert resubmit.json()["request_id"] != request_id

    cancelled_audit = await _audit_rows("school.kb.approval.cancelled", env.tenant_a)
    assert len(cancelled_audit) == 1


async def test_cross_tenant_submit_and_build_are_404(client: AsyncClient, env: KbEnv) -> None:
    """跨租版本 id：提交/构建一律 404（不泄露存在性）。"""

    headers = await _uploader_headers(client)
    _, version_id, _ = await _upload_one(client, headers)

    b_uploader_headers = await login(client, B_UPLOADER, school_code=SCHOOL_B)
    submit = await _submit(client, b_uploader_headers, version_id)
    assert submit.status_code == 404

    b_builder_headers = await login(client, B_BUILDER, school_code=SCHOOL_B)
    build = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/build", headers=b_builder_headers
    )
    assert build.status_code == 404


async def test_concurrent_decide_yields_exactly_one_winner(
    client: AsyncClient, env: KbEnv
) -> None:
    """两名审批人并发通过同一单：恰好一路成功，版本只前进一次，审计各记一行。"""

    headers = await _uploader_headers(client)
    _, version_id, _ = await _upload_one(client, headers)
    request_id = (await _submit(client, headers, version_id)).json()["request_id"]

    approver_id = await _user_id(KB_APPROVER)
    builder_id = await _user_id(KB_BUILDER)
    outcomes = await asyncio.gather(
        decide_version_approval(
            tenant_id=env.tenant_a,
            approval_request_id=request_id,
            actor_id=approver_id,
            action="approve",
            reason=None,
        ),
        decide_version_approval(
            tenant_id=env.tenant_a,
            approval_request_id=request_id,
            actor_id=builder_id,
            action="approve",
            reason=None,
        ),
        return_exceptions=True,
    )
    errors = [item for item in outcomes if isinstance(item, BaseException)]
    # 服务层并发用例不经过权限层（KB_BUILDER 无审批码也不影响本判据）：
    # 这里只钉「同一审批单只有一个最终动作」——恰好一路成功、一路 409。
    successes = [item for item in outcomes if not isinstance(item, BaseException)]
    assert len(successes) == 1, [
        (item.request.status, item.request.result) if not isinstance(item, BaseException) else repr(item)
        for item in outcomes
    ]
    assert len(errors) == 1, outcomes
    assert isinstance(errors[0], KbError)
    assert errors[0].code == "APPROVAL_ALREADY_DECIDED"

    request_rows = await rows(
        "select status, result from approval_requests where id = :rid", rid=request_id
    )
    assert request_rows[0]["status"] == "APPROVED"
    version_rows = await rows(
        "select status from kb_document_versions where id = :vid", vid=version_id
    )
    assert version_rows[0]["status"] == "APPROVED"
    approved_audit = await _audit_rows("school.kb.approval.approved", env.tenant_a)
    assert len(approved_audit) == 1


async def test_build_gate_409_for_non_approved(client: AsyncClient, env: KbEnv) -> None:
    """未审批创建构建：PENDING_APPROVAL/REJECTED 都 409 + 零任务行 + 拒绝审计。"""

    builder_headers = await login(client, KB_BUILDER)
    uploader_headers = await _uploader_headers(client)

    _, pending_version, _ = await _upload_one(client, uploader_headers)
    pending_build = await client.post(
        f"/api/v1/school/kb/document-versions/{pending_version}/build",
        headers=builder_headers,
    )
    assert pending_build.status_code == 409
    assert pending_build.json()["detail"]["code"] == "KB_NOT_APPROVED"
    jobs_after_pending = await rows("select count(*) as n from kb_build_jobs")
    assert jobs_after_pending[0]["n"] == 0

    _, reject_version, _ = await _upload_one(client, uploader_headers)
    request_id = (await _submit(client, uploader_headers, reject_version)).json()["request_id"]
    approver_headers = await login(client, KB_APPROVER)
    reject = await client.post(
        f"/api/v1/school/approvals/{request_id}/reject",
        headers=approver_headers,
        json={"reason": "暂不收录"},
    )
    assert reject.status_code == 200
    rejected_build = await client.post(
        f"/api/v1/school/kb/document-versions/{reject_version}/build",
        headers=builder_headers,
    )
    assert rejected_build.status_code == 409
    assert rejected_build.json()["detail"]["code"] == "KB_NOT_APPROVED"

    denied = await _audit_rows("school.kb.build.denied", env.tenant_a)
    assert [row["metadata_json"]["code"] for row in denied] == [
        "KB_NOT_APPROVED",
        "KB_NOT_APPROVED",
    ]
    jobs_after_denied = await rows("select count(*) as n from kb_build_jobs")
    assert jobs_after_denied[0]["n"] == 0


async def test_build_created_for_approved_and_second_conflicts(
    client: AsyncClient, env: KbEnv
) -> None:
    """APPROVED 创建构建 → BUILD_QUEUED + 审计；二次创建 409 且不产生第二行。"""

    uploader_headers = await _uploader_headers(client)
    _, version_id, _ = await _upload_one(client, uploader_headers)
    request_id = (await _submit(client, uploader_headers, version_id)).json()["request_id"]
    approver_headers = await login(client, KB_APPROVER)
    assert (await _decide(client, approver_headers, request_id, "approve")).status_code == 200

    builder_headers = await login(client, KB_BUILDER)
    first = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/build", headers=builder_headers
    )
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "BUILD_QUEUED"
    assert first.json()["attempt"] == 1

    job_rows = await rows(
        "select status, attempt, version_id from kb_build_jobs"
    )
    assert len(job_rows) == 1
    assert job_rows[0]["status"] == "BUILD_QUEUED"
    assert str(job_rows[0]["version_id"]) == version_id

    second = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/build", headers=builder_headers
    )
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "BUILD_ALREADY_ACTIVE"
    jobs_after_conflict = await rows("select count(*) as n from kb_build_jobs")
    assert jobs_after_conflict[0]["n"] == 1

    created_audit = await _audit_rows("school.kb.build.created", env.tenant_a)
    assert len(created_audit) == 1


async def test_metadata_update_audits_before_after_and_keeps_status(
    client: AsyncClient, env: KbEnv
) -> None:
    """元数据编辑：免审（版本状态不变）但审计前后值；无变化不审计；显式 null 清空描述。"""

    headers = await _uploader_headers(client)
    document_id, _version_id, original_title = await _upload_one(client, headers)

    update = await client.put(
        f"/api/v1/school/kb/documents/{document_id}",
        headers=headers,
        json={"title": "新标题", "tags": ["规章", "教学"], "description": "第一版说明"},
    )
    assert update.status_code == 200, update.text
    body = update.json()
    assert body["title"] == "新标题"
    assert body["tags"] == ["规章", "教学"]
    assert body["description"] == "第一版说明"
    assert body["status"] == "PENDING_APPROVAL"  # 版本/文档状态不变 = 免审

    document_rows = await rows(
        "select title, tags, description, status from kb_documents where id = :did",
        did=document_id,
    )
    assert document_rows[0]["title"] == "新标题"
    assert document_rows[0]["status"] == "PENDING_APPROVAL"

    audits = await _audit_rows("school.kb.metadata.updated", env.tenant_a)
    assert len(audits) == 1
    metadata = audits[0]["metadata_json"]
    assert metadata["before"]["title"] == original_title
    assert metadata["after"]["title"] == "新标题"
    assert metadata["after"]["description"] == "第一版说明"

    # 无变化的更新：不产生新审计
    noop = await client.put(
        f"/api/v1/school/kb/documents/{document_id}",
        headers=headers,
        json={"title": "新标题"},
    )
    assert noop.status_code == 200
    assert len(await _audit_rows("school.kb.metadata.updated", env.tenant_a)) == 1

    # 显式 null 清空描述
    clear = await client.put(
        f"/api/v1/school/kb/documents/{document_id}",
        headers=headers,
        json={"description": None},
    )
    assert clear.status_code == 200
    assert clear.json()["description"] is None
    audits_after_clear = await _audit_rows("school.kb.metadata.updated", env.tenant_a)
    assert len(audits_after_clear) == 2
    assert audits_after_clear[-1]["metadata_json"]["before"]["description"] == "第一版说明"
    assert audits_after_clear[-1]["metadata_json"]["after"]["description"] is None

    # 范围外/不存在 → 404
    missing = await client.put(
        f"/api/v1/school/kb/documents/{uuid4()}",
        headers=headers,
        json={"title": "无主文档"},
    )
    assert missing.status_code == 404


async def test_metadata_update_cross_tenant_is_404(client: AsyncClient, env: KbEnv) -> None:
    """跨租文档 id：元数据编辑 404。"""

    headers = await _uploader_headers(client)
    document_id, _, _ = await _upload_one(client, headers)
    b_headers = await login(client, B_UPLOADER, school_code=SCHOOL_B)
    response = await client.put(
        f"/api/v1/school/kb/documents/{document_id}",
        headers=b_headers,
        json={"title": "越权标题"},
    )
    assert response.status_code == 404


async def test_concurrent_build_creation_yields_exactly_one_job(
    client: AsyncClient, env: KbEnv
) -> None:
    """真并发创建构建：恰好一行任务、一路 201 一路 409（部分唯一索引兜底路径被钉住）。"""

    uploader = await _uploader_headers(client)
    _, version_id, _ = await _upload_one(client, uploader)
    request_id = (await _submit(client, uploader, version_id)).json()["request_id"]
    approver = await login(client, KB_APPROVER)
    assert (await _decide(client, approver, request_id, "approve")).status_code == 200

    builder = await login(client, KB_BUILDER)
    first, second = await asyncio.gather(
        client.post(
            f"/api/v1/school/kb/document-versions/{version_id}/build", headers=builder
        ),
        client.post(
            f"/api/v1/school/kb/document-versions/{version_id}/build", headers=builder
        ),
    )
    responses = [first, second]
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, 409], [(r.status_code, r.text) for r in responses]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["detail"]["code"] == "BUILD_ALREADY_ACTIVE"

    jobs = await rows("select count(*) as n from kb_build_jobs")
    assert jobs[0]["n"] == 1
    created_audit = await _audit_rows("school.kb.build.created", env.tenant_a)
    assert len(created_audit) == 1
    denied_audit = await _audit_rows("school.kb.build.denied", env.tenant_a)
    assert [row["metadata_json"]["code"] for row in denied_audit] == ["BUILD_ALREADY_ACTIVE"]


async def test_submit_with_max_length_title_succeeds(
    client: AsyncClient, env: KbEnv
) -> None:
    """审查 M1 边界：文档标题恰 200 字时提交审批不溢出（组合标题按 200 截断，不 500）。"""

    headers = await _uploader_headers(client)
    long_title = "长" * 200
    response = await upload(
        client,
        headers,
        key=f"kb02-max-title-{uuid4().hex[:12]}",
        files=[("max-title.md", MD, "text/markdown")],
        metadata='[{"title": "' + long_title + '"}]',
    )
    assert response.status_code == 201, response.text
    version_id = response.json()["documents"][0]["current_version"]["id"]

    submit = await _submit(client, headers, version_id)
    assert submit.status_code == 201, submit.text

    request_rows = await rows(
        "select title, payload_json from approval_requests where document_version_id = :vid",
        vid=version_id,
    )
    assert len(request_rows[0]["title"]) == 200
    assert request_rows[0]["title"].startswith("知识库文档审批：")
    # 完整标题不丢：快照的 document_title 仍是 200 字原文
    assert request_rows[0]["payload_json"]["document_title"] == long_title
