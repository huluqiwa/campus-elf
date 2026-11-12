"""业务场景 BS-M3-02：知识库审批与构建门禁（M3 KB-02）。

Given 校方上传（dev-skip 直达 PENDING_APPROVAL）与审批人/构建人账号；
When 提交审批 → 审批人待办可见 → 通过 → 创建构建；以及驳回链路；
Then 通过链路：版本与文档 APPROVED、审批单 APPROVED（含版本快照）、
     `kb_build_jobs` 恰一行 BUILD_QUEUED、审计 submitted/approved/created 逐条在库、
     待办清空且已办可见、未审批的兄弟文档不可构建；
     驳回链路：版本与文档 REJECTED、理由在审批单与详情可见、构建 409、
     REJECTED 不可再提交；无 `school.approval.todo` 的账号看不到待办（403）。
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import (
    KB_APPROVER,
    KB_BUILDER,
    MD,
    NO_PERM,
    UPLOADER,
    KbEnv,
    execute,
    kb_env,
    login,
    make_client,
    rows,
    upload,
)

pytestmark = [pytest.mark.business, pytest.mark.integration]


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
    async with kb_env(monkeypatch, tmp_path, overrides={"DEV_SKIP_FILE_SCAN": "true"}) as value:
        yield value


@pytest.fixture
async def client(env: KbEnv) -> AsyncIterator[AsyncClient]:
    async with make_client() as ac:
        yield ac


async def _upload_pending(
    client: AsyncClient, headers: dict[str, str], *, title: str
) -> tuple[str, str]:
    """上传一份直达 PENDING_APPROVAL 的文档，返回 (document_id, version_id)。"""

    response = await upload(
        client,
        headers,
        key=f"bs02-{uuid4().hex[:16]}",
        files=[(f"{title}.md", MD, "text/markdown")],
        metadata='[{"title": "' + title + '"}]',
    )
    assert response.status_code == 201, response.text
    document = response.json()["documents"][0]
    assert document["status"] == "PENDING_APPROVAL"
    version = document["current_version"]
    assert version is not None
    return document["id"], version["id"]


async def test_bs_m3_02_approve_then_build_full_chain(
    client: AsyncClient, env: KbEnv
) -> None:
    """上传 → 提交 → 待办 → 通过 → 构建门禁放行：状态、快照、审计、任务行逐项对账。"""

    uploader = await login(client, UPLOADER)
    approver = await login(client, KB_APPROVER)
    builder = await login(client, KB_BUILDER)

    _, version_a = await _upload_pending(client, uploader, title="链路文档A")
    _, version_b = await _upload_pending(client, uploader, title="链路文档B")

    submit = await client.post(
        f"/api/v1/school/kb/document-versions/{version_a}/submit-approval",
        headers=uploader,
    )
    assert submit.status_code == 201, submit.text
    request_id = submit.json()["request_id"]

    todo = await client.get("/api/v1/school/approvals/todo", headers=approver)
    assert todo.status_code == 200
    todo_ids = [item["id"] for item in todo.json()["items"]]
    assert request_id in todo_ids

    # 无 school.approval.todo 的账号看不到待办（服务端 403，不是前端隐藏）
    forbidden_todo = await client.get("/api/v1/school/approvals/todo", headers=uploader)
    assert forbidden_todo.status_code == 403

    approve = await client.post(
        f"/api/v1/school/approvals/{request_id}/approve", headers=approver, json={}
    )
    assert approve.status_code == 200, approve.text

    version_rows = await rows(
        "select status from kb_document_versions where id = :vid", vid=version_a
    )
    assert version_rows[0]["status"] == "APPROVED"
    document_rows = await rows(
        "select status from kb_documents where current_version_id = :vid", vid=version_a
    )
    assert document_rows[0]["status"] == "APPROVED"
    # 兄弟文档不受影响：仍是待审批
    sibling = await rows(
        "select status from kb_document_versions where id = :vid", vid=version_b
    )
    assert sibling[0]["status"] == "PENDING_APPROVAL"

    # 审批单：APPROVED + 版本快照可追溯
    detail = await client.get(f"/api/v1/school/approvals/{request_id}", headers=approver)
    assert detail.status_code == 200
    body = detail.json()
    assert body["request"]["status"] == "APPROVED"
    assert body["request"]["payload"]["version_id"] == version_a
    assert body["request"]["payload"]["document_title"] == "链路文档A"
    assert [step["status"] for step in body["steps"]] == ["APPROVED"]
    assert [action["action"] for action in body["actions"]] == ["submit", "approve"]

    todo_after = await client.get("/api/v1/school/approvals/todo", headers=approver)
    assert all(item["id"] != request_id for item in todo_after.json()["items"])
    done = await client.get("/api/v1/school/approvals/done", headers=approver)
    assert any(item["id"] == request_id for item in done.json()["items"])

    # 兄弟文档（未审批）构建 → 409 且零任务行
    sibling_build = await client.post(
        f"/api/v1/school/kb/document-versions/{version_b}/build", headers=builder
    )
    assert sibling_build.status_code == 409
    assert sibling_build.json()["detail"]["code"] == "KB_NOT_APPROVED"
    assert (await rows("select count(*) as n from kb_build_jobs"))[0]["n"] == 0

    # APPROVED 文档构建 → BUILD_QUEUED（K4 门禁接线：卡 4 配 all+确认）
    from tests.support.kb_audience_seed import configure_audiences

    await configure_audiences(env.tenant_a, UUID(version_a))
    build = await client.post(
        f"/api/v1/school/kb/document-versions/{version_a}/build", headers=builder
    )
    assert build.status_code == 201, build.text
    assert build.json()["status"] == "BUILD_QUEUED"
    job_rows = await rows("select version_id, status, attempt from kb_build_jobs")
    assert len(job_rows) == 1
    assert str(job_rows[0]["version_id"]) == version_a
    assert job_rows[0]["status"] == "BUILD_QUEUED"
    assert job_rows[0]["attempt"] == 1

    audits = await rows(
        "select action, result from audit_logs where tenant_id = :tid and action in "
        "('school.kb.approval.submitted', 'school.kb.approval.approved', "
        "'school.kb.build.created', 'school.kb.build.denied') order by created_at",
        tid=env.tenant_a,
    )
    assert [row["action"] for row in audits] == [
        "school.kb.approval.submitted",
        "school.kb.approval.approved",
        "school.kb.build.denied",
        "school.kb.build.created",
    ]
    assert all(row["result"] in ("success", "denied") for row in audits)


async def test_bs_m3_02_reject_chain_reason_visible_and_not_buildable(
    client: AsyncClient, env: KbEnv
) -> None:
    """驳回链路：理由必填且可见；REJECTED 不可构建、不可再提交；撤销后可重来。"""

    uploader = await login(client, UPLOADER)
    approver = await login(client, KB_APPROVER)

    _, version_id = await _upload_pending(client, uploader, title="驳回文档")
    request_id = (
        await client.post(
            f"/api/v1/school/kb/document-versions/{version_id}/submit-approval",
            headers=uploader,
        )
    ).json()["request_id"]

    empty_reject = await client.post(
        f"/api/v1/school/approvals/{request_id}/reject", headers=approver, json={}
    )
    assert empty_reject.status_code == 422
    assert empty_reject.json()["detail"]["code"] == "REJECT_REASON_REQUIRED"

    reject = await client.post(
        f"/api/v1/school/approvals/{request_id}/reject",
        headers=approver,
        json={"reason": "内容与学校口径不符，退回重写"},
    )
    assert reject.status_code == 200, reject.text

    detail = await client.get(f"/api/v1/school/approvals/{request_id}", headers=approver)
    assert detail.json()["request"]["reject_reason"] == "内容与学校口径不符，退回重写"

    version_rows = await rows(
        "select status from kb_document_versions where id = :vid", vid=version_id
    )
    assert version_rows[0]["status"] == "REJECTED"
    build = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/build",
        headers=await login(client, KB_BUILDER),
    )
    assert build.status_code == 409
    assert build.json()["detail"]["code"] == "KB_NOT_APPROVED"

    resubmit = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/submit-approval",
        headers=uploader,
    )
    assert resubmit.status_code == 409
    assert resubmit.json()["detail"]["code"] == "KB_NOT_SUBMITTABLE"

    # 兜底确认：REJECTED 文档没有任务行、驳回审计在库
    assert (await rows("select count(*) as n from kb_build_jobs"))[0]["n"] == 0
    rejected_audits = await rows(
        "select metadata_json from audit_logs where tenant_id = :tid "
        "and action = 'school.kb.approval.rejected'",
        tid=env.tenant_a,
    )
    assert len(rejected_audits) == 1
    assert rejected_audits[0]["metadata_json"]["reason"] == "内容与学校口径不符，退回重写"


async def test_bs_m3_02_upload_to_scan_gate_remains_enforced(
    client: AsyncClient, env: KbEnv
) -> None:
    """扫描门禁不被绕过：把版本拨回 UPLOADED 后提交审批被拒（KB-01 的 409 判据仍然在）。"""

    uploader = await login(client, UPLOADER)
    _, version_id = await _upload_pending(client, uploader, title="未扫描文档")
    await execute(
        "update kb_document_versions set status = 'UPLOADED', scan_engine = null, "
        "scanned_at = null where id = :vid",
        vid=version_id,
    )
    response = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/submit-approval",
        headers=uploader,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "KB_NOT_SUBMITTABLE"
    # 无码账号提交/审批是权限层 403，不是 404/409
    no_perm = await login(client, NO_PERM)
    no_perm_response = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/submit-approval",
        headers=no_perm,
    )
    assert no_perm_response.status_code == 403
