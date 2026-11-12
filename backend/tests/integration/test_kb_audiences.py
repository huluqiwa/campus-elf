"""M3 KB-09A 人群 ACL 集成测试（真实 PG + 真实 Qdrant；构建/嵌入 fake ports）。

覆盖简报 §4.3 红→绿清单：
- 三端点：GET 初始空 / PUT 全量替换+置未确认+审计 / confirm 置确认+审计；
- confirm 守卫：空集 409 `ACL_REQUIRED`、重复 confirm 409 `ACL_ALREADY_CONFIRMED`；
- PUT 校验 422 族：类型白名单 / all 混配 / 缺 id / 对象不存在 / 跨租对象；
- 跨租与权限：跨租 version 404（不泄露）、无码 403；
- 门禁（spec :307 / TD-302）：构建创建与发布同形 409 `ACL_REQUIRED`/`ACL_UNCONFIRMED`
  + 拒绝审计（build.denied / publish.denied 既有动作）+ 零副作用；
- 执行期二次校验：取件时未确认 → job FAILED `ACL_UNCONFIRMED`（确认后重构建可达）；
- payload（D2-B）：构建写入逐 chunk `acl_tokens`；PUT 置未确认 → set_payload 清空
  （恒不召回 fail-closed）、confirm → set_payload 写新 tokens。
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient, Response

from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_audience_seed import configure_audiences
from tests.support.kb_env import (
    B_UPLOADER,
    KB_ACL_MANAGER,
    KB_B_PUBLISHER,
    KB_BUILDER,
    KB_PUBLISHER,
    NO_PERM,
    SCHOOL_B,
    KbEnv,
    execute,
    kb_env,
    login,
    make_client,
    rows,
)
from tests.support.kb_publish_seed import seed_test_passed_version

pytestmark = [pytest.mark.integration]

AUDIENCES_URL = "/api/v1/school/kb/document-versions/{version_id}/audiences"
BUILD_URL = "/api/v1/school/kb/document-versions/{version_id}/build"
PUBLISH_URL = "/api/v1/school/kb/versions/{version_id}/publish"


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


async def _manager(client: AsyncClient) -> dict[str, str]:
    return await login(client, KB_ACL_MANAGER)


async def _get(
    client: AsyncClient, headers: dict[str, str], version_id: UUID
) -> dict[str, Any]:
    response = await client.get(AUDIENCES_URL.format(version_id=version_id), headers=headers)
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


async def _put(
    client: AsyncClient,
    headers: dict[str, str],
    version_id: UUID,
    audiences: list[dict[str, Any]],
) -> Response:
    return await client.put(
        AUDIENCES_URL.format(version_id=version_id), headers=headers, json={"audiences": audiences}
    )


async def _confirm(client: AsyncClient, headers: dict[str, str], version_id: UUID) -> Response:
    return await client.post(
        AUDIENCES_URL.format(version_id=version_id) + "/confirm", headers=headers
    )


async def _seed_department(tenant_id: UUID) -> UUID:
    """造一个同租部门行（department 类 audience 的存在性校验对象）。"""

    department_id = uuid4()
    await execute(
        """
        insert into departments (id, tenant_id, name, path, status)
        values (:id, :t, :name, :path, 'active')
        """,
        id=str(department_id),
        t=str(tenant_id),
        name=f"财务处-{department_id.hex[:8]}",
        path=str(department_id),
    )
    return department_id


async def _seed_role_id(tenant_id: UUID) -> str:
    """取一个同租角色 id（roles 已随开通种下；role 类 audience 的校验对象）。"""

    result = await rows("select id from roles where tenant_id = :t limit 1", t=str(tenant_id))
    assert result, "租户角色未种子化"
    return str(result[0]["id"])


async def _cross_tenant_user_id() -> str:
    result = await rows(
        "select id from users where login_id = :lid", lid=B_UPLOADER
    )
    assert result
    return str(result[0]["id"])


async def _scroll_payload_tokens(collection: str) -> list[str]:
    from qdrant_client import AsyncQdrantClient

    from app.config import get_settings

    settings = get_settings()
    client_q = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        scrolled, _ = await client_q.scroll(
            collection, limit=100, with_payload=True, with_vectors=False
        )
    finally:
        await client_q.close()
    assert scrolled
    tokens = {tuple(sorted((point.payload or {}).get("acl_tokens") or [])) for point in scrolled}
    assert len(tokens) == 1, f"payload acl_tokens 不一致：{tokens}"
    return list(next(iter(tokens)))


async def test_get_audiences_initial_empty(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await seed_test_passed_version(
        env, monkeypatch, with_audience=False, run_build=False
    )
    body = await _get(client, await _manager(client), version_id)
    assert body["version_id"] == str(version_id)
    assert body["audiences"] == []
    assert body["acl_confirmed"] is False
    assert body["acl_confirmed_by"] is None
    assert body["acl_confirmed_at"] is None


async def test_put_replaces_and_unconfirms_with_audit(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    department_id = await _seed_department(env.tenant_a)
    role_id = await _seed_role_id(env.tenant_a)
    _doc, version_id = await seed_test_passed_version(env, monkeypatch)  # 默认 all 已确认
    manager = await _manager(client)

    response = await _put(
        client,
        manager,
        version_id,
        [
            {"audience_type": "department", "audience_id": str(department_id)},
            {"audience_type": "role", "audience_id": role_id},
        ],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [(a["audience_type"], a["audience_id"]) for a in body["audiences"]] == [
        ("department", str(department_id)),
        ("role", role_id),
    ]
    assert body["acl_confirmed"] is False

    audits = await rows(
        "select action, metadata_json from audit_logs where action = 'school.kb.acl.updated' "
        "and tenant_id = :tid and resource_id = :rid",
        tid=str(env.tenant_a),
        rid=str(version_id),
    )
    assert len(audits) == 1
    after = audits[0]["metadata_json"]["after"]
    assert ("department", str(department_id)) in [(a[0], a[1]) for a in after]

    # 重置回默认（不影响其他用例的租户级行数断言）
    await configure_audiences(env.tenant_a, version_id)


async def test_put_then_confirm_flow_with_audit(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    department_id = await _seed_department(env.tenant_a)
    _doc, version_id = await seed_test_passed_version(env, monkeypatch)
    manager = await _manager(client)

    put = await _put(
        client,
        manager,
        version_id,
        [{"audience_type": "department", "audience_id": str(department_id)}],
    )
    assert put.status_code == 200, put.text
    confirm = await _confirm(client, manager, version_id)
    assert confirm.status_code == 200, confirm.text
    body = confirm.json()
    assert body["acl_confirmed"] is True
    assert body["acl_confirmed_at"] is not None
    assert [(a["audience_type"], a["audience_id"]) for a in body["audiences"]] == [
        ("department", str(department_id))
    ]

    audits = await rows(
        "select action, metadata_json from audit_logs where action = 'school.kb.acl.confirmed' "
        "and tenant_id = :tid and resource_id = :rid",
        tid=str(env.tenant_a),
        rid=str(version_id),
    )
    assert len(audits) == 1
    assert audits[0]["metadata_json"]["audience_count"] == 1


async def test_confirm_empty_rejected_409(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await seed_test_passed_version(
        env, monkeypatch, with_audience=False, run_build=False
    )
    put = await _put(client, await _manager(client), version_id, [])
    assert put.status_code == 200, put.text
    confirm = await _confirm(client, await _manager(client), version_id)
    assert confirm.status_code == 409, confirm.text
    assert confirm.json()["detail"]["code"] == "ACL_REQUIRED"


async def test_repeat_confirm_rejected_409(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await seed_test_passed_version(env, monkeypatch)
    confirm = await _confirm(client, await _manager(client), version_id)
    assert confirm.status_code == 409, confirm.text
    assert confirm.json()["detail"]["code"] == "ACL_ALREADY_CONFIRMED"


async def test_put_all_mixed_rejected_422(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await seed_test_passed_version(
        env, monkeypatch, with_audience=False, run_build=False
    )
    response = await _put(
        client,
        await _manager(client),
        version_id,
        [
            {"audience_type": "all", "audience_id": None},
            {"audience_type": "user", "audience_id": str(uuid4())},
        ],
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "AUDIENCE_ALL_MIXED"


async def test_put_unknown_type_rejected_422(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await seed_test_passed_version(
        env, monkeypatch, with_audience=False, run_build=False
    )
    response = await _put(
        client,
        await _manager(client),
        version_id,
        [{"audience_type": "group", "audience_id": str(uuid4())}],
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "AUDIENCE_TYPE_INVALID"


async def test_put_missing_id_rejected_422(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await seed_test_passed_version(
        env, monkeypatch, with_audience=False, run_build=False
    )
    response = await _put(
        client,
        await _manager(client),
        version_id,
        [{"audience_type": "department", "audience_id": ""}],
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "AUDIENCE_ID_REQUIRED"


async def test_put_nonexistent_audience_rejected_422(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await seed_test_passed_version(
        env, monkeypatch, with_audience=False, run_build=False
    )
    response = await _put(
        client,
        await _manager(client),
        version_id,
        [{"audience_type": "department", "audience_id": str(uuid4())}],
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "AUDIENCE_NOT_FOUND"


async def test_put_cross_tenant_audience_rejected_422(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """跨租 audience_id（B 租用户 id）→ 422 同文（spec 验收 :319 不跨租命中）。"""

    _doc, version_id = await seed_test_passed_version(
        env, monkeypatch, with_audience=False, run_build=False
    )
    foreign_user = await _cross_tenant_user_id()
    response = await _put(
        client,
        await _manager(client),
        version_id,
        [{"audience_type": "user", "audience_id": foreign_user}],
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "AUDIENCE_NOT_FOUND"


async def test_cross_tenant_version_404(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await seed_test_passed_version(env, monkeypatch)
    b_manager = await login(client, KB_B_PUBLISHER, school_code=SCHOOL_B)
    # GET（B 发布人持 view）→ 404 不泄露存在性
    response = await client.get(AUDIENCES_URL.format(version_id=version_id), headers=b_manager)
    assert response.status_code == 404, response.text
    # PUT/confirm（B 发布人无 acl.manage）→ 权限先于存在性判定 → 403（同样不泄露）
    put = await _put(
        client,
        b_manager,
        version_id,
        [{"audience_type": "all", "audience_id": None}],
    )
    assert put.status_code == 403, put.text
    confirm = await _confirm(client, b_manager, version_id)
    assert confirm.status_code == 403, confirm.text


async def test_no_permission_403(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await seed_test_passed_version(
        env, monkeypatch, with_audience=False, run_build=False
    )
    no_perm = await login(client, NO_PERM)
    get = await client.get(AUDIENCES_URL.format(version_id=version_id), headers=no_perm)
    assert get.status_code == 403, get.text
    put = await _put(
        client,
        no_perm,
        version_id,
        [{"audience_type": "all", "audience_id": None}],
    )
    assert put.status_code == 403, put.text
    confirm = await _confirm(client, no_perm, version_id)
    assert confirm.status_code == 403, confirm.text


async def test_build_gate_required(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await seed_test_passed_version(
        env, monkeypatch, with_audience=False, run_build=False
    )
    builder = await login(client, KB_BUILDER)
    response = await client.post(BUILD_URL.format(version_id=version_id), headers=builder)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "ACL_REQUIRED"
    jobs = await rows(
        "select id from kb_build_jobs where version_id = :v", v=str(version_id)
    )
    assert jobs == []
    audits = await rows(
        "select metadata_json from audit_logs where action = 'school.kb.build.denied' "
        "and tenant_id = :tid",
        tid=str(env.tenant_a),
    )
    assert any(a["metadata_json"]["code"] == "ACL_REQUIRED" for a in audits)


async def test_build_gate_unconfirmed(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await seed_test_passed_version(
        env, monkeypatch, with_audience=False, run_build=False
    )
    await configure_audiences(env.tenant_a, version_id, confirmed=False)
    builder = await login(client, KB_BUILDER)
    response = await client.post(BUILD_URL.format(version_id=version_id), headers=builder)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "ACL_UNCONFIRMED"
    jobs = await rows("select id from kb_build_jobs where version_id = :v", v=str(version_id))
    assert jobs == []


async def test_build_succeeds_after_confirm_with_payload_tokens(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """确认后构建可达；payload 逐 chunk 携带 acl_tokens（D2-B / spec 验收 :321）。"""

    from app.kb.build_task import build_kb_version
    from app.kb.indexer import collection_name
    from tests.support.kb_publish_seed import patch_ports

    _doc, version_id = await seed_test_passed_version(
        env, monkeypatch, with_audience=False, run_build=False, markdown="# 手册\n\n正文内容。\n"
    )
    await configure_audiences(env.tenant_a, version_id)  # ("all", None) 已确认
    builder = await login(client, KB_BUILDER)
    build = await client.post(BUILD_URL.format(version_id=version_id), headers=builder)
    assert build.status_code == 201, build.text

    patch_ports(monkeypatch, _fake_parser("# 手册\n\n正文内容。\n"))
    result = await build_kb_version({}, str(env.tenant_a), str(version_id))
    assert result["status"] == "ready", result

    tokens = await _scroll_payload_tokens(collection_name(env.tenant_a, version_id))
    assert tokens == ["all"]


async def test_build_task_runtime_gate_fails_unconfirmed(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """执行期二次校验：取件时未确认 → job FAILED ACL_UNCONFIRMED；确认后重构建可达。"""

    _doc, version_id = await seed_test_passed_version(
        env, monkeypatch, with_audience=False, run_build=False, markdown="# 手册\n\n正文内容。\n"
    )
    await configure_audiences(env.tenant_a, version_id)  # 先确认（直落库种子路径）
    manager = await _manager(client)
    put = await _put(
        client, manager, version_id, [{"audience_type": "all", "audience_id": None}]
    )  # PUT 置未确认
    assert put.status_code == 200, put.text

    from tests.support.kb_env import UPLOADER

    uploader_id = await rows("select id from users where login_id = :l", l=UPLOADER)
    await execute(
        """
        insert into kb_build_jobs (id, tenant_id, version_id, status, attempt, retry_count, created_by)
        values (:id, :t, :v, 'BUILD_QUEUED', 1, 0, :by)
        """,
        id=str(uuid4()),
        t=str(env.tenant_a),
        v=str(version_id),
        by=str(uploader_id[0]["id"]),
    )

    from app.kb.build_task import build_kb_version

    result = await build_kb_version({}, str(env.tenant_a), str(version_id))
    assert result["status"] == "failed", result
    assert result["error_code"] == "ACL_UNCONFIRMED"

    job = await rows(
        "select status, error_code from kb_build_jobs where version_id = :v order by created_at desc",
        v=str(version_id),
    )
    assert job[0]["status"] == "FAILED"
    assert job[0]["error_code"] == "ACL_UNCONFIRMED"

    # 确认后重试可达（可重试语义：FAILED → confirm → retry 端点 → READY；
    # create_build 只收 APPROVED，FAILED 版本的恢复路径是既有 retry 端点）
    confirm = await _confirm(client, manager, version_id)
    assert confirm.status_code == 200, confirm.text
    from tests.support.kb_publish_seed import patch_ports

    patch_ports(monkeypatch, _fake_parser("# 手册\n\n正文内容。\n"))
    failed_job = await rows(
        "select id from kb_build_jobs where version_id = :v and status = 'FAILED'",
        v=str(version_id),
    )
    assert failed_job
    builder = await login(client, KB_BUILDER)
    retry = await client.post(
        f"/api/v1/school/kb/build-jobs/{failed_job[0]['id']}/retry", headers=builder
    )
    assert retry.status_code == 200, retry.text
    result2 = await build_kb_version({}, str(env.tenant_a), str(version_id))
    assert result2["status"] == "ready", result2


async def test_publish_gate_required_and_unconfirmed(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """发布门禁与构建侧同形（TD-302）：TEST_PASSED 版本清人群/置未确认 → 409。"""

    _doc, version_id = await seed_test_passed_version(env, monkeypatch)  # 构建期已配 all
    publisher = await login(client, KB_PUBLISHER)

    # 清空人群（未确认）→ ACL_REQUIRED
    put_empty = await _put(client, await _manager(client), version_id, [])
    assert put_empty.status_code == 200, put_empty.text
    publish = await client.post(PUBLISH_URL.format(version_id=version_id), headers=publisher)
    assert publish.status_code == 409, publish.text
    assert publish.json()["detail"]["code"] == "ACL_REQUIRED"

    # 配置但未确认 → ACL_UNCONFIRMED
    put = await _put(
        client,
        await _manager(client),
        version_id,
        [{"audience_type": "all", "audience_id": None}],
    )
    assert put.status_code == 200, put.text
    publish2 = await client.post(PUBLISH_URL.format(version_id=version_id), headers=publisher)
    assert publish2.status_code == 409, publish2.text
    assert publish2.json()["detail"]["code"] == "ACL_UNCONFIRMED"

    denied = await rows(
        "select metadata_json from audit_logs where action = 'school.kb.publish.denied' "
        "and tenant_id = :tid",
        tid=str(env.tenant_a),
    )
    codes = [a["metadata_json"]["code"] for a in denied]
    assert "ACL_REQUIRED" in codes and "ACL_UNCONFIRMED" in codes

    # 确认后发布可达
    confirm = await _confirm(client, await _manager(client), version_id)
    assert confirm.status_code == 200, confirm.text
    publish3 = await client.post(PUBLISH_URL.format(version_id=version_id), headers=publisher)
    assert publish3.status_code == 200, publish3.text


async def test_acl_payload_sync_on_put_and_confirm(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """D2-B 同步：PUT 置未确认 → payload 清空（恒不召回）；confirm → 新 tokens。"""

    from app.kb.indexer import collection_name

    department_id = await _seed_department(env.tenant_a)
    _doc, version_id = await seed_test_passed_version(env, monkeypatch)  # 构建期 payload=["all"]
    collection = collection_name(env.tenant_a, version_id)
    assert await _scroll_payload_tokens(collection) == ["all"]

    put = await _put(
        client,
        await _manager(client),
        version_id,
        [{"audience_type": "department", "audience_id": str(department_id)}],
    )
    assert put.status_code == 200, put.text
    assert await _scroll_payload_tokens(collection) == []  # 未确认 → 清空（fail-closed）

    confirm = await _confirm(client, await _manager(client), version_id)
    assert confirm.status_code == 200, confirm.text
    assert await _scroll_payload_tokens(collection) == [f"department:{department_id}"]


def _fake_parser(markdown: str) -> Any:
    """局部 fake parser（kb_publish_seed.FakeParser 收单个 ParsedDocument）。"""

    from app.kb.parser import ParsedDocument
    from tests.support.kb_publish_seed import FakeParser

    return FakeParser(ParsedDocument(markdown=markdown))
