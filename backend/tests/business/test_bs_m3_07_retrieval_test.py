"""业务场景 BS-M3-07：知识库检索测试（M3 KB-07A）——真实 PG/Redis/Qdrant。

Given 真实基础设施（PG/Redis/Qdrant；解析 fake、嵌入词袋 fake 两侧同源、
重排 fake——真实引擎证据在 BS-M3-03，TD-295 口径）；
When HTTP 全链：上传（dev-skip 扫描）→ 提交审批 → 审批通过 → 创建构建 →
     worker 管线 READY → 检索测试端点；
Then（对照 spec §KB-06/07/08 验收 :294-300「Given 检索测试 query，When 运行，
Then 展示 top-k 分数与最终引用，能定位『为什么召回』」）：
- 默认快查：检索结果 top-1 = 目标表格子块，dense/sparse/RRF 分数齐全，
  rerank_status=disabled，最终引用标记正确；
- 「为什么召回」逐路可解释：目标块 dense/sparse 均第 1 名（词袋同正文）；
- 「最终引用定位到分块」：行内元数据（chunk_index/section_path/preview）与
  PG 对账，表格整块文本在检索结果里原样保真（表格未被拆坏的检索面证据）；
- 跨租拒绝：B 租主体检索 A 租版本 → 404 不泄露存在性。
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
    KB_APPROVER,
    KB_B_CHUNK_VIEWER,
    KB_BUILDER,
    KB_CHUNK_VIEWER,
    SCHOOL_B,
    UPLOADER,
    KbEnv,
    kb_env,
    login,
    make_client,
    rows,
    upload,
)
from tests.support.kb_publish_seed import FakeParser, patch_ports
from tests.support.kb_retrieval_seed import (
    TokenOverlapEmbedder,
    patch_query_embedder,
)

pytestmark = [pytest.mark.business, pytest.mark.integration]

URL = "/api/v1/school/kb/versions/{version_id}/retrieval-test"

RICH_MARKDOWN = """# 操作手册

## 第一章 安装

安装步骤1完成。安装步骤2完成。安装步骤3完成。安装步骤4完成。安装步骤5完成。

## 第二章 配置

配置项1已生效。配置项2已生效。配置项3已生效。配置项4已生效。配置项5已生效。
配置项6已生效。配置项7已生效。配置项8已生效。配置项9已生效。配置项10已生效。
配置项11已生效。配置项12已生效。配置项13已生效。配置项14已生效。配置项15已生效。
配置项16已生效。配置项17已生效。配置项18已生效。配置项19已生效。配置项20已生效。

| 参数 | 默认值 |
| --- | --- |
| 超时 | 30 |
| 重试 | 3 |

```python
print("hello kb")
```

## 第三章 常见问题

问题1已解决。问题2已解决。问题3已解决。问题4已解决。问题5已解决。
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
        monkeypatch,
        tmp_path,
        overrides={
            "QDRANT_URL": "http://127.0.0.1:56333",
            "DEV_SKIP_FILE_SCAN": "true",
        },
    ) as value:
        yield value


@pytest.fixture
async def client(env: KbEnv) -> AsyncIterator[AsyncClient]:
    async with make_client() as ac:
        yield ac


async def _chain_to_ready(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> tuple[UUID, UUID]:
    """HTTP 全链到 READY：上传 → 提交 → 通过 → 创建构建 → worker 管线。

    构建侧嵌入用词袋确定性向量（与查询侧同源，patch_ports 之后覆盖生效）。
    """

    patch_ports(monkeypatch, FakeParser(ParsedDocument(markdown=RICH_MARKDOWN)))
    from app.kb import build_task

    monkeypatch.setattr(build_task, "_build_embedder", lambda settings: TokenOverlapEmbedder())
    uploader_headers = await login(client, UPLOADER)
    uploaded = await upload(
        client,
        uploader_headers,
        key=f"bs07-{uuid4().hex[:16]}",
        files=[("bs07.md", RICH_MARKDOWN.encode("utf-8"), "text/markdown")],
        metadata='[{"title": "检索测试业务场景文档", "tags": ["手册"]}]',
    )
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()["documents"][0]
    assert document["status"] == "PENDING_APPROVAL"
    doc_id = UUID(document["id"])
    version_id = UUID(document["current_version"]["id"])

    submitted = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/submit-approval",
        headers=uploader_headers,
    )
    assert submitted.status_code == 201, submitted.text
    request_id = submitted.json()["request_id"]

    approver_headers = await login(client, KB_APPROVER)
    approved = await client.post(
        f"/api/v1/school/approvals/{request_id}/approve",
        headers=approver_headers,
        json={},
    )
    assert approved.status_code == 200, approved.text

    built = await client.post(
        f"/api/v1/school/kb/document-versions/{version_id}/build",
        headers=await login(client, KB_BUILDER),
    )
    assert built.status_code == 201, built.text

    result = await build_kb_version({}, str(env.tenant_a), str(version_id))
    assert result["status"] == "ready", result
    return doc_id, version_id


async def test_full_chain_retrieval_test_business_outputs(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """检索测试的业务输出：分数齐全、逐路可解释、最终引用定位到分块（表格保真）。"""

    _doc_id, version_id = await _chain_to_ready(env, monkeypatch, client)
    patch_query_embedder(monkeypatch)
    table_chunks = await rows(
        "select id, chunk_index, section_path, text from kb_chunks "
        "where document_version_id = :v and chunk_type = 'child' and text like :pattern "
        "order by chunk_index",
        v=str(version_id),
        pattern="%| 重试 | 3 |%",
    )
    assert table_chunks, "表格子块应存在且被索引"
    target = table_chunks[0]

    headers = await login(client, KB_CHUNK_VIEWER)
    resp = await client.post(
        URL.format(version_id=version_id), json={"query": target["text"]}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 业务输出 1：默认快查（无重排），分数三路齐全
    assert body["rerank_status"] == "disabled"
    assert body["with_rerank"] is False
    results = body["results"]
    assert results
    top = results[0]
    assert top["chunk_id"] == str(target["id"]), "表格子块应是检索 top-1"
    assert top["dense"] is not None and top["dense"]["rank"] == 1
    assert top["sparse"] is not None and top["sparse"]["rank"] == 1
    assert top["rrf_score"] > 0
    assert top["final_rank"] == 1 and top["is_final_reference"] is True

    # 业务输出 2：「为什么召回」+「最终引用定位到分块」——元数据与 PG 对账
    assert top["chunk_index"] == target["chunk_index"]
    assert top["section_path"] == target["section_path"]
    # 表格整块在检索结果里原样保真（表头与数据行同块，表格未被拆坏的检索面证据）
    assert "| 参数 | 默认值 |" in top["text_preview"]
    assert "| 重试 | 3 |" in top["text_preview"]
    assert [row["final_rank"] for row in results] == list(range(1, len(results) + 1))
    assert sum(1 for row in results if row["is_final_reference"]) == min(10, len(results))
    assert all(row["dense"] is not None or row["sparse"] is not None for row in results)


async def test_cross_tenant_retrieval_test_is_denied(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """跨租拒绝：B 租主体检索 A 租版本 → 404 同码（spec 跨租验收）。"""

    _doc_id, version_id = await _chain_to_ready(env, monkeypatch, client)
    b_headers = await login(client, KB_B_CHUNK_VIEWER, school_code=SCHOOL_B)
    resp = await client.post(
        URL.format(version_id=version_id), json={"query": "任意问题"}, headers=b_headers
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "VERSION_NOT_FOUND"
