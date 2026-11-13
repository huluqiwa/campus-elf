"""M3 KB-09B 检索强制过滤与人群解析集成测试（真实 PG + 真实 Qdrant；解析 fake、
嵌入词袋两侧同源）。

覆盖简报 `docs/design-briefs/M3-KB-09.md` §3.2（决策卡 B1-B4，2026-11-14 拍板）：
- filter 三处下沉：fusion/dense/sparse 旁路同 filter（spy 断言参数；ignore_acl=None）；
- 视角三参数：默认操作者 / as_user_id 同租校验（不存在/跨租/已删除 404 同文）/
  as_audiences 结构校验 422 与二选一冲突 422；
- ignore_acl：无码 403 零审计；有码放行 + 强制审计（query sha256+长度，不带原文）；
- 未确认版本零召回（PUT 清空 payload——KB-09A 语义在检索面的表现钉住）；
- 批量运行 as_audiences 持久化 + 执行期生效；默认发起人视角 fail-closed（宁严勿松）；
- all 夹具零漂移背书（配 all + 用户恒注入 all → 既有断言形态零漂移）。
"""

import hashlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.kb import retrieval_test, test_task
from app.kb.build_task import build_kb_version
from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import (
    KB_ACL_MANAGER,
    KB_ACL_UNFILTERED,
    KB_B_CHUNK_VIEWER,
    KB_CHUNK_VIEWER,
    KB_TEACHER_VIEWER,
    KB_TEST_RUNNER,
    NO_PERM,
    UPLOADER,
    KbEnv,
    execute,
    kb_env,
    login,
    make_client,
    rows,
)
from tests.support.kb_publish_seed import (
    seed_test_passed_version as _seed_test_passed_version,
)
from tests.support.kb_retrieval_seed import TokenOverlapEmbedder, patch_query_embedder

pytestmark = [pytest.mark.integration]

URL = "/api/v1/school/kb/versions/{version_id}/retrieval-test"
RUNS_URL = "/api/v1/school/kb/versions/{version_id}/tests"
AUDIENCES_URL = "/api/v1/school/kb/document-versions/{version_id}/audiences"

MARKDOWN = """# 财务报销手册

## 第一章 差旅报销

差旅报销需要发票和审批单。报销流程第一步提交申请。第二步部门负责人审批。第三步财务复核打款。

## 第二章 招待报销

招待费报销需注明事由。招待标准按级别执行。超额部分不予报销。

## 第三章 报销时限

当月费用当月报销。跨季度需特别审批。年底统一清零。
"""


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


# ---------------------------------------------------------------------------
# 种子助手
# ---------------------------------------------------------------------------


async def _user_id(login_id: str) -> UUID:
    result = await rows("select id from users where login_id = :l", l=login_id)
    assert result, f"测试账号未种子化：{login_id}"
    return UUID(str(result[0]["id"]))


async def _seed_department(
    tenant_id: UUID, *, name: str, parent_id: UUID | None = None
) -> UUID:
    """部门行（path 物化路径格式 `/{parent…}/{own}`，含自身结尾）。"""

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


async def _seed_teacher_profile(tenant_id: UUID, login_id: str, department_id: UUID | None) -> None:
    """教师档案行（teachers 与 users 1:1；department_id 可空 = 未归属）。"""

    user_id = await _user_id(login_id)
    await execute(
        """
        insert into teachers (id, tenant_id, user_id, employee_no, name, department_id)
        values (gen_random_uuid(), :t, :u, :no, '教师', :d)
        """,
        t=str(tenant_id),
        u=str(user_id),
        no=f"T-{uuid4().hex[:10]}",
        d=str(department_id) if department_id else None,
    )


async def _seed_class(tenant_id: UUID, *, grade: str, head_teacher: str | None = None) -> UUID:
    """班级行（班主任权威在 classes.head_teacher_id）。"""

    class_id = uuid4()
    head = str(await _user_id(head_teacher)) if head_teacher else None
    await execute(
        """
        insert into classes (id, tenant_id, entry_year, grade, name, status, head_teacher_id)
        values (:id, :t, 2024, :g, :n, 'active', :h)
        """,
        id=str(class_id),
        t=str(tenant_id),
        g=grade,
        n=f"高一{class_id.hex[:4]}班",
        h=head,
    )
    return class_id


async def _build_version(
    env: KbEnv,
    monkeypatch: pytest.MonkeyPatch,
    *,
    audiences: tuple[tuple[str, str | None], ...],
    markdown: str = MARKDOWN,
) -> tuple[UUID, UUID]:
    """APPROVED 版本 → 配置人群并确认 → 真实构建 READY（payload 携带人群 token）。

    与 seed_test_passed_version 的差异：人群在构建**前**配置（非 all 默认档），
    payload token 即所配人群（构建期 D2-B 事实源）。
    """

    doc, version_id = await _seed_test_passed_version(
        env,
        monkeypatch,
        title="ACL 检索用文档",
        markdown=markdown,
        embedder=TokenOverlapEmbedder(),
        with_audience=False,
        run_build=False,
    )
    from tests.support.kb_audience_seed import configure_audiences

    await configure_audiences(env.tenant_a, version_id, audiences=audiences)
    creator = str(await _user_id(UPLOADER))
    await execute(
        """
        insert into kb_build_jobs (id, tenant_id, version_id, status, attempt, retry_count, created_by)
        values (:id, :t, :v, 'BUILD_QUEUED', 1, 0, :c)
        """,
        id=str(uuid4()),
        t=str(env.tenant_a),
        v=str(version_id),
        c=creator,
    )
    from app.kb import build_task

    monkeypatch.setattr(build_task, "_build_embedder", lambda settings: TokenOverlapEmbedder())
    result = await build_kb_version({}, str(env.tenant_a), str(version_id))
    assert result["status"] == "ready", result
    return doc, version_id


async def _child_chunks(version_id: UUID) -> list[dict[str, Any]]:
    return await rows(
        "select id, chunk_index, text from kb_chunks "
        "where document_version_id = :v and chunk_type = 'child' order by chunk_index",
        v=str(version_id),
    )


async def _post_retrieval(
    client: AsyncClient,
    headers: dict[str, str],
    version_id: UUID,
    payload: dict[str, Any],
) -> Any:
    resp = await client.post(URL.format(version_id=version_id), json=payload, headers=headers)
    return resp


# ---------------------------------------------------------------------------
# 1. 零漂移背书：配 all 夹具 + 默认操作者视角 → 既有断言形态零漂移
# ---------------------------------------------------------------------------


async def test_default_view_on_all_fixture_zero_drift(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await _seed_test_passed_version(
        env, monkeypatch, title="all 夹具文档", markdown=MARKDOWN, embedder=TokenOverlapEmbedder()
    )
    patch_query_embedder(monkeypatch)
    headers = await login(client, KB_CHUNK_VIEWER)

    resp = await client.post(
        URL.format(version_id=version_id), json={"query": "报销流程", "top_k": 5}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["acl_filter_applied"] is True
    assert "all" in body["resolved_audience_tokens"]
    assert body["results"], "all 夹具 + 恒注入 all → 必有召回（零漂移背书）"
    for row in body["results"]:
        assert row["acl_matched_tokens"] == ["all"]


# ---------------------------------------------------------------------------
# 2. filter 生效：部门人群成员命中 / 无关操作者零召回（不泄露）
# ---------------------------------------------------------------------------


async def test_department_audience_member_hits_admin_misses(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    dept = await _seed_department(env.tenant_a, name="财务处")
    _doc, version_id = await _build_version(
        env, monkeypatch, audiences=(("department", str(dept)),)
    )
    patch_query_embedder(monkeypatch)
    chunks = await _child_chunks(version_id)
    assert chunks
    query = chunks[0]["text"]

    member_headers = await login(client, KB_TEACHER_VIEWER)
    await _seed_teacher_profile(env.tenant_a, KB_TEACHER_VIEWER, dept)
    resp = await client.post(
        URL.format(version_id=version_id), json={"query": query}, headers=member_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"], "部门成员（教师）必命中"
    assert body["results"][0]["acl_matched_tokens"] == [f"department:{dept}"]

    admin_headers = await login(client, KB_CHUNK_VIEWER)
    resp = await client.post(
        URL.format(version_id=version_id), json={"query": query}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"] == [], "部门外操作者零召回（不提示存在）"
    assert body["acl_filter_applied"] is True
    assert body["resolved_audience_tokens"], "操作者自身 token 非空（all 恒注入）"


async def test_parent_department_audience_hits_child_member(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """spec :316 层级：人群配在父部门 → 子部门成员（祖先链命中）可见。"""

    parent = await _seed_department(env.tenant_a, name="财务处")
    child = await _seed_department(env.tenant_a, name="报销科", parent_id=parent)
    _doc, version_id = await _build_version(
        env, monkeypatch, audiences=(("department", str(parent)),)
    )
    patch_query_embedder(monkeypatch)
    chunks = await _child_chunks(version_id)
    assert chunks

    await _seed_teacher_profile(env.tenant_a, KB_TEACHER_VIEWER, child)
    headers = await login(client, KB_TEACHER_VIEWER)
    resp = await client.post(
        URL.format(version_id=version_id),
        json={"query": chunks[0]["text"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"], "子部门成员命中父部门配置（祖先链）"
    assert f"department:{parent}" in body["results"][0]["acl_matched_tokens"]


async def test_class_audience_head_teacher_hits(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    class_id = await _seed_class(env.tenant_a, grade="高一", head_teacher=KB_TEACHER_VIEWER)
    _doc, version_id = await _build_version(
        env, monkeypatch, audiences=(("class", str(class_id)),)
    )
    patch_query_embedder(monkeypatch)
    chunks = await _child_chunks(version_id)
    assert chunks

    headers = await login(client, KB_TEACHER_VIEWER)
    resp = await client.post(
        URL.format(version_id=version_id),
        json={"query": chunks[0]["text"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"], "班主任命中班级人群"
    assert f"class:{class_id}" in body["results"][0]["acl_matched_tokens"]


# ---------------------------------------------------------------------------
# 3. 未确认版本：PUT 清空 payload → 检索面零召回（KB-09A 语义钉住）
# ---------------------------------------------------------------------------


async def test_unconfirmed_version_zero_recall(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    dept = await _seed_department(env.tenant_a, name="财务处")
    _doc, version_id = await _build_version(
        env, monkeypatch, audiences=(("department", str(dept)),)
    )
    patch_query_embedder(monkeypatch)
    chunks = await _child_chunks(version_id)
    assert chunks

    # PUT 全量替换（置未确认）→ service set_payload 清空 acl_tokens
    manager_headers = await login(client, KB_ACL_MANAGER)
    put = await client.put(
        AUDIENCES_URL.format(version_id=version_id),
        headers=manager_headers,
        json={"audiences": [{"audience_type": "department", "audience_id": str(dept)}]},
    )
    assert put.status_code == 200, put.text

    headers = await login(client, KB_TEACHER_VIEWER)
    await _seed_teacher_profile(env.tenant_a, KB_TEACHER_VIEWER, dept)
    resp = await client.post(
        URL.format(version_id=version_id),
        json={"query": chunks[0]["text"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"] == [], "未确认版本恒不召回（fail-closed，即使成员查询）"


# ---------------------------------------------------------------------------
# 4. as_audiences 模拟视角与结构校验
# ---------------------------------------------------------------------------


async def test_as_audiences_simulates_membership(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    dept = await _seed_department(env.tenant_a, name="财务处")
    _doc, version_id = await _build_version(
        env, monkeypatch, audiences=(("department", str(dept)),)
    )
    patch_query_embedder(monkeypatch)
    chunks = await _child_chunks(version_id)
    assert chunks

    headers = await login(client, KB_CHUNK_VIEWER)
    resp = await client.post(
        URL.format(version_id=version_id),
        json={"query": chunks[0]["text"], "as_audiences": [f"department:{dept}"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"], "模拟部门视角命中"
    assert body["resolved_audience_tokens"] == [f"department:{dept}"]
    assert body["results"][0]["acl_matched_tokens"] == [f"department:{dept}"]

    miss = await client.post(
        URL.format(version_id=version_id),
        json={"query": chunks[0]["text"], "as_audiences": [f"department:{uuid4()}"]},
        headers=headers,
    )
    assert miss.status_code == 200
    assert miss.json()["results"] == [], "模拟不存在人群 → 零召回（fail-closed）"


async def test_as_audiences_invalid_and_conflict_422(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await _seed_test_passed_version(
        env, monkeypatch, markdown=MARKDOWN, embedder=TokenOverlapEmbedder()
    )
    headers = await login(client, KB_CHUNK_VIEWER)
    some_user = await _user_id(KB_CHUNK_VIEWER)

    for payload, code in (
        ({"query": "q", "as_audiences": ["group:x"]}, "AUDIENCE_TOKEN_INVALID"),
        ({"query": "q", "as_audiences": ["department"]}, "AUDIENCE_TOKEN_INVALID"),
        ({"query": "q", "as_audiences": ["department:not-a-uuid"]}, "AUDIENCE_TOKEN_INVALID"),
        (
            {"query": "q", "as_audiences": [f"user:{some_user}", f"user:{some_user}"]},
            "AUDIENCE_TOKEN_DUPLICATE",
        ),
        (
            {
                "query": "q",
                "as_user_id": str(some_user),
                "as_audiences": ["all"],
            },
            "RETRIEVAL_TEST_PERSPECTIVE_CONFLICT",
        ),
    ):
        resp = await client.post(
            URL.format(version_id=version_id), json=payload, headers=headers
        )
        assert resp.status_code == 422, f"{payload} 应 422：{resp.text}"
        assert resp.json()["detail"]["code"] == code, f"{payload} → {code}"


# ---------------------------------------------------------------------------
# 5. as_user_id 视角与跨租/不存在/已删除 404 同文
# ---------------------------------------------------------------------------


async def test_as_user_perspective_and_not_found_same_code(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    dept = await _seed_department(env.tenant_a, name="财务处")
    _doc, version_id = await _build_version(
        env, monkeypatch, audiences=(("department", str(dept)),)
    )
    patch_query_embedder(monkeypatch)
    chunks = await _child_chunks(version_id)
    assert chunks

    teacher_user = await _user_id(KB_TEACHER_VIEWER)
    await _seed_teacher_profile(env.tenant_a, KB_TEACHER_VIEWER, dept)
    headers = await login(client, KB_CHUNK_VIEWER)
    resp = await client.post(
        URL.format(version_id=version_id),
        json={"query": chunks[0]["text"], "as_user_id": str(teacher_user)},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"], "以部门成员用户视角命中"
    assert f"department:{dept}" in body["results"][0]["acl_matched_tokens"]

    b_user = await _user_id(KB_B_CHUNK_VIEWER)
    missing = await client.post(
        URL.format(version_id=version_id),
        json={"query": "q", "as_user_id": str(uuid4())},
        headers=headers,
    )
    cross = await client.post(
        URL.format(version_id=version_id),
        json={"query": "q", "as_user_id": str(b_user)},
        headers=headers,
    )
    for resp in (missing, cross):
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["code"] == "RETRIEVAL_TEST_AS_USER_NOT_FOUND"
        assert resp.json()["detail"]["message"] == missing.json()["detail"]["message"]

    # 已删除用户同文 404（不泄露状态差异）
    await execute(
        "update users set status = 'deleted' where login_id = :l and tenant_id = :t",
        l=NO_PERM,
        t=str(env.tenant_a),
    )
    deleted_user = await _user_id(NO_PERM)
    gone = await client.post(
        URL.format(version_id=version_id),
        json={"query": "q", "as_user_id": str(deleted_user)},
        headers=headers,
    )
    assert gone.status_code == 404
    assert gone.json()["detail"]["message"] == missing.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# 6. ignore_acl：无码 403 零审计；有码放行 + 强制审计（query 摘要，不带原文）
# ---------------------------------------------------------------------------


async def test_ignore_acl_without_permission_403_no_audit(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await _seed_test_passed_version(
        env, monkeypatch, markdown=MARKDOWN, embedder=TokenOverlapEmbedder()
    )
    headers = await login(client, KB_CHUNK_VIEWER)
    resp = await client.post(
        URL.format(version_id=version_id),
        json={"query": "报销", "ignore_acl": True},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "RETRIEVAL_TEST_UNFILTERED_FORBIDDEN"
    audits = await rows(
        "select metadata_json from audit_logs where action = 'school.kb.retrieval_test.unfiltered' "
        "and resource_id = :v",
        v=str(version_id),
    )
    assert audits == []


async def test_ignore_acl_returns_unfiltered_with_audit(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    dept = await _seed_department(env.tenant_a, name="财务处")
    _doc, version_id = await _build_version(
        env, monkeypatch, audiences=(("department", str(dept)),)
    )
    patch_query_embedder(monkeypatch)
    chunks = await _child_chunks(version_id)
    assert chunks
    query = chunks[0]["text"]

    # 对照：同一操作者（school_admin）过滤视角零召回
    headers = await login(client, KB_ACL_UNFILTERED)
    filtered = await client.post(
        URL.format(version_id=version_id), json={"query": query}, headers=headers
    )
    assert filtered.status_code == 200
    assert filtered.json()["results"] == []

    resp = await client.post(
        URL.format(version_id=version_id),
        json={"query": query, "ignore_acl": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["acl_filter_applied"] is False
    assert body["results"], "ignore_acl 返回未过滤候选"
    assert all(row["acl_matched_tokens"] == [] for row in body["results"])

    audits = await rows(
        "select metadata_json from audit_logs where action = 'school.kb.retrieval_test.unfiltered' "
        "and resource_id = :v",
        v=str(version_id),
    )
    assert len(audits) == 1
    metadata = audits[0]["metadata_json"]
    assert metadata["query_sha256"] == hashlib.sha256(query.encode("utf-8")).hexdigest()
    assert metadata["query_length"] == len(query)
    assert query not in json.dumps(metadata), "审计不得带 query 原文"


# ---------------------------------------------------------------------------
# 7. filter 三处下沉（fusion + dense/sparse 旁路同 filter；spy 断言）
# ---------------------------------------------------------------------------


async def test_filter_sinks_to_all_three_routes(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await _seed_test_passed_version(
        env, monkeypatch, markdown=MARKDOWN, embedder=TokenOverlapEmbedder()
    )
    patch_query_embedder(monkeypatch)
    calls: list[tuple[str, Any]] = []
    # 真实实现直接从 indexer 取（retrieval_test 模块的导入项按 no_implicit_reexport
    # 不对外可见，mypy strict 口径）；行为等价——retrieval_test.search_points 即该函数
    from app.kb.indexer import search_dense as real_dense
    from app.kb.indexer import search_points as real_search_points
    from app.kb.indexer import search_sparse as real_sparse

    async def spy(
        name: str,
        fn: Callable[..., Awaitable[list[dict[str, Any]]]],
        *args: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        calls.append((name, kwargs.get("query_filter")))
        return await fn(*args, **kwargs)

    monkeypatch.setattr(
        retrieval_test,
        "search_points",
        lambda *a, **kw: spy("fusion", real_search_points, *a, **kw),
    )
    monkeypatch.setattr(
        retrieval_test, "search_dense", lambda *a, **kw: spy("dense", real_dense, *a, **kw)
    )
    monkeypatch.setattr(
        retrieval_test, "search_sparse", lambda *a, **kw: spy("sparse", real_sparse, *a, **kw)
    )
    headers = await login(client, KB_CHUNK_VIEWER)
    resp = await client.post(
        URL.format(version_id=version_id), json={"query": "报销"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    names = [name for name, _ in calls]
    assert names == ["fusion", "dense", "sparse"], names
    for _name, query_filter in calls:
        assert query_filter is not None, "三路查询都必须带 ACL filter"
        assert query_filter.should[0].key == "acl_tokens"
        assert "all" in query_filter.should[0].match.any

    calls.clear()
    resp = await client.post(
        URL.format(version_id=version_id),
        json={"query": "报销", "ignore_acl": True},
        headers=await login(client, KB_ACL_UNFILTERED),
    )
    assert resp.status_code == 200, resp.text
    assert calls, "ignore_acl 仍发起三路查询"
    for _name, query_filter in calls:
        assert query_filter is None, "ignore_acl 不带 filter"


# ---------------------------------------------------------------------------
# 8. 批量运行：as_audiences 持久化 + 执行期生效；默认发起人视角 fail-closed
# ---------------------------------------------------------------------------


async def _seed_test_set(
    env: KbEnv, version_id: UUID, *, question: str, expected_chunk_id: UUID
) -> UUID:
    creator = str(await _user_id(UPLOADER))
    test_set_id = uuid4()
    await execute(
        """
        insert into kb_test_sets (id, tenant_id, document_version_id, name,
                                  file_object_key, file_format, case_count, created_by)
        values (:id, :t, :v, 'ACL 判分集', 'kb/test.csv', 'csv', 1, :c)
        """,
        id=str(test_set_id),
        t=str(env.tenant_a),
        v=str(version_id),
        c=creator,
    )
    await execute(
        """
        insert into kb_test_cases (id, tenant_id, test_set_id, row_no, question,
                                   expected_chunk_ids, is_adversarial)
        values (:id, :t, :s, 1, :q, CAST(:e AS jsonb), false)
        """,
        id=str(uuid4()),
        t=str(env.tenant_a),
        s=str(test_set_id),
        q=question,
        e=json.dumps([str(expected_chunk_id)]),
    )
    return test_set_id


async def test_batch_run_as_audiences_applied_and_default_fail_closed(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    dept = await _seed_department(env.tenant_a, name="财务处")
    _doc, version_id = await _build_version(
        env, monkeypatch, audiences=(("department", str(dept)),)
    )
    chunks = await _child_chunks(version_id)
    assert chunks
    test_set_id = await _seed_test_set(
        env, version_id, question=chunks[0]["text"], expected_chunk_id=UUID(str(chunks[0]["id"]))
    )
    monkeypatch.setattr(test_task, "_build_embedder_for_test", lambda s: TokenOverlapEmbedder())
    headers = await login(client, KB_TEST_RUNNER)

    # ① as_audiences 显式视角：模拟部门成员 → 期望块命中 → TEST_PASSED
    resp = await client.post(
        RUNS_URL.format(version_id=version_id),
        data={
            "test_set_id": str(test_set_id),
            "as_audiences": json.dumps([f"department:{dept}"]),
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["run_id"]
    run_rows = await rows(
        "select as_audiences from kb_test_runs where id = :r", r=str(run_id)
    )
    assert run_rows and run_rows[0]["as_audiences"] == [f"department:{dept}"]
    result = await test_task.run_kb_test({}, str(env.tenant_a), str(version_id))
    assert result["status"] == "test_passed", result

    # ② 默认视角（发起人 school_admin：role/user/all）→ 部门文档零召回 → 判 FAIL（宁严勿松）
    resp = await client.post(
        RUNS_URL.format(version_id=version_id),
        data={"test_set_id": str(test_set_id)},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    run_rows = await rows(
        "select as_audiences from kb_test_runs where id = :r", r=str(resp.json()["run_id"])
    )
    assert run_rows and run_rows[0]["as_audiences"] is None
    result = await test_task.run_kb_test({}, str(env.tenant_a), str(version_id))
    assert result["status"] == "test_failed", result
    assert result["pass_rate"] == 0.0

    # ③ 发起后账号被删 → 执行期默认视角空集 FAIL（宁严勿松；与检索测试 as_user_id
    # 删态 404 同口径——审查 m1；发起须在删号前，否则 HTTP 层 401）
    resp = await client.post(
        RUNS_URL.format(version_id=version_id),
        data={"test_set_id": str(test_set_id)},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    await execute(
        "update users set status = 'deleted' where login_id = :l and tenant_id = :t",
        l=KB_TEST_RUNNER,
        t=str(env.tenant_a),
    )
    result = await test_task.run_kb_test({}, str(env.tenant_a), str(version_id))
    assert result["status"] == "test_failed", result
    assert result["pass_rate"] == 0.0
