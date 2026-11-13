"""业务场景 BS-M3-10：检索强制过滤与人群解析（M3 KB-09B，简报 §3.2 决策卡 B1-B4）。

Given 真实基础设施（PG/Redis/Qdrant；解析/嵌入 fake ports——真实引擎证据在
BS-M3-03/BS-M3-03b，TD-295 口径）；
When HTTP 全链：上传（dev-skip 扫描）→ 审批 → 配置人群（HTTP PUT+confirm）→
     构建 READY → 检索测试 HTTP（视角过滤）；
Then（对照 spec §KB-09 验收 :316-318）：
- **层级（:316）**：人群配在父部门 → 子部门成员（教师）检索命中且响应带回
  `department:父` 命中 token；部门外操作者（校管）零召回——不召回、不引用、
  不提示存在（结果为空而非 403/404）；
- **并集（:317）**：教师任教两个班且其中一个班在 ACL 内 → 命中（任教关系
  并集进 token 集）；
- **收紧即时（:318）**：PUT 全量替换置未确认 → payload 即时清空 → 下一次
  检索请求立即零召回（不被旧结果放宽）→ 重新确认后恢复。
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.kb.build_task import build_kb_version
from app.kb.parser import ParsedDocument
from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import (
    KB_ACL_MANAGER,
    KB_APPROVER,
    KB_CHUNK_VIEWER,
    KB_TEACHER_VIEWER,
    UPLOADER,
    KbEnv,
    execute,
    kb_env,
    login,
    make_client,
    rows,
    upload,
)
from tests.support.kb_publish_seed import (
    MARKDOWN,
    FakeParser,
    patch_ports,
)
from tests.support.kb_retrieval_seed import TokenOverlapEmbedder, patch_query_embedder

pytestmark = [pytest.mark.business, pytest.mark.integration]

AUDIENCES_URL = "/api/v1/school/kb/document-versions/{version_id}/audiences"
BUILD_URL = "/api/v1/school/kb/document-versions/{version_id}/build"
RETRIEVAL_URL = "/api/v1/school/kb/versions/{version_id}/retrieval-test"


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


async def _wait_scan_pending(
    client: AsyncClient, uploader: dict[str, str], document_id: str, version_id: str
) -> None:
    """等扫描到 PENDING_APPROVAL；扫描 worker 不在测试进程内则直置（BS-03 同款）。"""

    import asyncio

    for _ in range(40):
        detail = await client.get(f"/api/v1/school/kb/documents/{document_id}", headers=uploader)
        if detail.json()["status"] == "PENDING_APPROVAL":
            return
        await asyncio.sleep(0.5)
    await execute(
        "update kb_document_versions set status = 'PENDING_APPROVAL', scan_engine = 'clamav',"
        " scanned_at = now() where id = :vid and status = 'UPLOADED'",
        vid=version_id,
    )
    await execute(
        "update kb_documents set status = 'PENDING_APPROVAL' where id = :doc and status = 'UPLOADED'",
        doc=document_id,
    )


async def _approved_version(
    env: KbEnv,
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    *,
    key_prefix: str,
) -> str:
    """上传 → 审批 → APPROVED（fake parser；KB-09A BS-09 同链）。"""

    uploader = await login(client, UPLOADER)
    approver = await login(client, KB_APPROVER)
    response = await upload(
        client,
        uploader,
        key=f"{key_prefix}-{uuid4().hex[:16]}",
        files=[("acl-retrieval.md", MARKDOWN.encode("utf-8"), "text/markdown")],
    )
    assert response.status_code == 201, response.text
    document = response.json()["documents"][0]
    version_id = str(document["current_version"]["id"])
    await _wait_scan_pending(client, uploader, str(document["id"]), version_id)
    submit = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/submit-approval", headers=uploader
    )
    assert submit.status_code == 201, submit.text
    approve = await client.post(
        f"/api/v1/school/approvals/{submit.json()['request_id']}/approve",
        headers=approver,
        json={},
    )
    assert approve.status_code == 200, approve.text
    return version_id


async def _seed_department(tenant_id: UUID, *, name: str, parent_id: UUID | None = None) -> UUID:
    department_id = uuid4()
    path = f"{parent_id}/{department_id}" if parent_id else f"/{department_id}"
    await execute(
        """
        insert into departments (id, tenant_id, name, parent_id, path, status)
        values (:id, :t, :name, :p, :path, 'active')
        """,
        id=str(department_id),
        t=str(tenant_id),
        name=f"{name}-{department_id.hex[:8]}",
        p=str(parent_id) if parent_id else None,
        path=path,
    )
    return department_id


async def _user_id(login_id: str) -> UUID:
    result = await rows("select id from users where login_id = :l", l=login_id)
    assert result, f"测试账号未种子化：{login_id}"
    return UUID(str(result[0]["id"]))


async def _seed_class(tenant_id: UUID, *, grade: str, name_hint: str) -> UUID:
    class_id = uuid4()
    await execute(
        """
        insert into classes (id, tenant_id, entry_year, grade, name, status)
        values (:id, :t, 2024, :g, :n, 'active')
        """,
        id=str(class_id),
        t=str(tenant_id),
        g=grade,
        n=f"{name_hint}-{class_id.hex[:4]}班",
    )
    return class_id


async def _seed_teacher_teaching(tenant_id: UUID, login_id: str, class_ids: list[UUID]) -> None:
    """教师档案 + 任教关系（teacher_class_courses；课程行按需补）。"""

    user_id = await _user_id(login_id)
    await execute(
        """
        insert into teachers (id, tenant_id, user_id, employee_no, name)
        values (gen_random_uuid(), :t, :u, :no, '教师')
        """,
        t=str(tenant_id),
        u=str(user_id),
        no=f"T-{uuid4().hex[:10]}",
    )
    teacher_row = (
        await rows("select id from teachers where user_id = :u and tenant_id = :t",
                   u=str(user_id), t=str(tenant_id))
    )[0]
    for class_id in class_ids:
        course_id = uuid4()
        await execute(
            """
            insert into courses (id, tenant_id, code, name) values (:id, :t, :c, '数学')
            """,
            id=str(course_id),
            t=str(tenant_id),
            c=f"MATH-{course_id.hex[:8]}",
        )
        await execute(
            """
            insert into teacher_class_courses (id, tenant_id, teacher_id, class_id, course_id, term)
            values (gen_random_uuid(), :t, :teacher, :class_id, :course_id, '2025-2026-1')
            """,
            t=str(tenant_id),
            teacher=str(teacher_row["id"]),
            class_id=str(class_id),
            course_id=str(course_id),
        )


async def _build_ready(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, version_id: str
) -> None:
    """人群已在 DB（HTTP PUT+confirm 由用例执行）→ 插构建任务 → 真实构建 READY。"""

    patch_ports(monkeypatch, FakeParser(ParsedDocument(markdown=MARKDOWN)))
    from app.kb import build_task

    monkeypatch.setattr(build_task, "_build_embedder", lambda settings: TokenOverlapEmbedder())
    creator = str(await _user_id(UPLOADER))
    await execute(
        """
        insert into kb_build_jobs (id, tenant_id, version_id, status, attempt, retry_count, created_by)
        values (:id, :t, :v, 'BUILD_QUEUED', 1, 0, :c)
        """,
        id=str(uuid4()),
        t=str(env.tenant_a),
        v=version_id,
        c=creator,
    )
    result = await build_kb_version({}, str(env.tenant_a), version_id)
    assert result["status"] == "ready", result


async def test_bs_m3_10_department_hierarchy_member_hits_and_tightening(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """spec :316 层级 + :318 收紧即时：父部门配置 → 子部门成员命中；PUT 后下一次
    请求立即按新 ACL。"""

    parent = await _seed_department(env.tenant_a, name="财务处")
    child = await _seed_department(env.tenant_a, name="报销科", parent_id=parent)
    await execute(
        """
        insert into teachers (id, tenant_id, user_id, employee_no, name, department_id)
        values (gen_random_uuid(), :t, :u, :no, '教师', :d)
        """,
        t=str(env.tenant_a),
        u=str(await _user_id(KB_TEACHER_VIEWER)),
        no=f"T-{uuid4().hex[:10]}",
        d=str(child),
    )

    version_id = await _approved_version(env, monkeypatch, client, key_prefix="bs10a")

    manager = await login(client, KB_ACL_MANAGER)
    put = await client.put(
        AUDIENCES_URL.format(version_id=version_id),
        headers=manager,
        json={"audiences": [{"audience_type": "department", "audience_id": str(parent)}]},
    )
    assert put.status_code == 200, put.text
    confirm = await client.post(
        AUDIENCES_URL.format(version_id=version_id) + "/confirm", headers=manager
    )
    assert confirm.status_code == 200, confirm.text
    await _build_ready(env, monkeypatch, version_id)

    patch_query_embedder(monkeypatch)
    chunks = await rows(
        "select id, text from kb_chunks where document_version_id = :v and chunk_type = 'child' "
        "order by chunk_index limit 1",
        v=version_id,
    )
    assert chunks
    query = chunks[0]["text"]

    # 子部门成员（教师）命中父部门配置——祖先链展开（:316）
    teacher = await login(client, KB_TEACHER_VIEWER)
    resp = await client.post(
        RETRIEVAL_URL.format(version_id=version_id), json={"query": query}, headers=teacher
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"], "子部门成员命中父部门配置（层级包含）"
    assert f"department:{parent}" in body["results"][0]["acl_matched_tokens"]
    assert f"department:{child}" in body["resolved_audience_tokens"]
    assert "all" in body["resolved_audience_tokens"]

    # 部门外操作者（校管）零召回——不提示存在（结果为空而非 403/404）
    admin = await login(client, KB_CHUNK_VIEWER)
    resp = await client.post(
        RETRIEVAL_URL.format(version_id=version_id), json={"query": query}, headers=admin
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"] == []
    assert body["acl_filter_applied"] is True

    # 收紧即时（:318）：PUT 全量替换置未确认 → payload 清空 → 教师下一次请求立即零召回
    put2 = await client.put(
        AUDIENCES_URL.format(version_id=version_id),
        headers=manager,
        json={"audiences": [{"audience_type": "department", "audience_id": str(parent)}]},
    )
    assert put2.status_code == 200, put2.text
    resp = await client.post(
        RETRIEVAL_URL.format(version_id=version_id), json={"query": query}, headers=teacher
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == [], "未确认窗口下一次请求立即按新 ACL（不被旧结果放宽）"

    # 重新确认 → 恢复命中（可重试语义）
    confirm2 = await client.post(
        AUDIENCES_URL.format(version_id=version_id) + "/confirm", headers=manager
    )
    assert confirm2.status_code == 200, confirm2.text
    resp = await client.post(
        RETRIEVAL_URL.format(version_id=version_id), json={"query": query}, headers=teacher
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"], "重新确认后恢复召回"

    from qdrant_client import AsyncQdrantClient

    from app.config import get_settings
    from app.kb.indexer import collection_name

    settings = get_settings()
    client_q = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        await client_q.delete_collection(collection_name(env.tenant_a, UUID(version_id)))
    finally:
        await client_q.close()


async def test_bs_m3_10_teacher_two_classes_union(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """spec :317 并集：教师任教两个班且其中一个班在 ACL 内 → 命中。"""

    class_a = await _seed_class(env.tenant_a, grade="高一", name_hint="甲")
    class_b = await _seed_class(env.tenant_a, grade="高一", name_hint="乙")
    await _seed_teacher_teaching(env.tenant_a, KB_TEACHER_VIEWER, [class_a, class_b])

    version_id = await _approved_version(env, monkeypatch, client, key_prefix="bs10b")

    manager = await login(client, KB_ACL_MANAGER)
    put = await client.put(
        AUDIENCES_URL.format(version_id=version_id),
        headers=manager,
        json={"audiences": [{"audience_type": "class", "audience_id": str(class_b)}]},
    )
    assert put.status_code == 200, put.text
    confirm = await client.post(
        AUDIENCES_URL.format(version_id=version_id) + "/confirm", headers=manager
    )
    assert confirm.status_code == 200, confirm.text
    await _build_ready(env, monkeypatch, version_id)

    patch_query_embedder(monkeypatch)
    chunks = await rows(
        "select id, text from kb_chunks where document_version_id = :v and chunk_type = 'child' "
        "order by chunk_index limit 1",
        v=version_id,
    )
    assert chunks
    query = chunks[0]["text"]

    teacher = await login(client, KB_TEACHER_VIEWER)
    resp = await client.post(
        RETRIEVAL_URL.format(version_id=version_id), json={"query": query}, headers=teacher
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"], "任教两班其一在 ACL 内 → 命中（并集语义）"
    assert f"class:{class_b}" in body["results"][0]["acl_matched_tokens"]
    assert f"class:{class_a}" in body["resolved_audience_tokens"], "任教关系并集进 token 集"

    # 校管（无班级关系）零召回
    admin = await login(client, KB_CHUNK_VIEWER)
    resp = await client.post(
        RETRIEVAL_URL.format(version_id=version_id), json={"query": query}, headers=admin
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == []

    from qdrant_client import AsyncQdrantClient

    from app.config import get_settings
    from app.kb.indexer import collection_name

    settings = get_settings()
    client_q = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        await client_q.delete_collection(collection_name(env.tenant_a, UUID(version_id)))
    finally:
        await client_q.close()
