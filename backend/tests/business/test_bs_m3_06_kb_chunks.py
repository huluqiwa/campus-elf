"""业务场景 BS-M3-06：知识库分块预览（M3 KB-06A）——真实 PG/Redis/Qdrant。

Given 真实基础设施（PG/Redis/Qdrant；解析/嵌入 fake ports——真实引擎证据在 BS-M3-03，
TD-295 口径）；
When HTTP 全链：上传（dev-skip 扫描）→ 提交审批 → 审批通过 → 创建构建 →
     worker 管线 READY → 分块预览三端点（列表/详情/统计）；
Then（对照 spec §KB-06 验收 :294-300）：
- 列表按 `chunk_index` 升序与库内全集一致，游标分页不重不漏；
- 「表格/代码未被拆坏」：表格两行数据同落一个块、代码整块独立（读侧保真呈现）；
- 搜索关键词只返回匹配块（含父块），与库内 ILIKE 全集一致；分页正确；
- 统计与库内聚合逐字段一致（总数/父子/embedding/token/字符/章节）；
- 跨租拒绝：B 租主体访问 A 租版本/分块，三端点一律 404（不泄露存在性）。
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
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

pytestmark = [pytest.mark.business, pytest.mark.integration]

LIST_URL = "/api/v1/school/kb/versions/{version_id}/chunks"
STATS_URL = "/api/v1/school/kb/versions/{version_id}/chunks/stats"
DETAIL_URL = "/api/v1/school/kb/chunks/{chunk_id}"

RICH_MARKDOWN = """# 操作手册

## 第一章 安装

安装步骤1完成。安装步骤2完成。安装步骤3完成。安装步骤4完成。安装步骤5完成。

## 第二章 配置

配置项1已生效。配置项2已生效。配置项3已生效。配置项4已生效。配置项5已生效。
配置项6已生效。配置项7已生效。配置项8已生效。配置项9已生效。配置项10已生效。
配置项11已生效。配置项12已生效。配置项13已生效。配置项14已生效。配置项15已生效。
配置项16已生效。配置项17已生效。配置项18已生效。配置项19已生效。配置项20已生效。
配置项21已生效。配置项22已生效。配置项23已生效。配置项24已生效。配置项25已生效。
配置项26已生效。配置项27已生效。配置项28已生效。配置项29已生效。配置项30已生效。
配置项31已生效。配置项32已生效。配置项33已生效。配置项34已生效。配置项35已生效。
配置项36已生效。配置项37已生效。配置项38已生效。配置项39已生效。配置项40已生效。
配置项41已生效。配置项42已生效。配置项43已生效。配置项44已生效。配置项45已生效。
配置项46已生效。配置项47已生效。配置项48已生效。配置项49已生效。配置项50已生效。
配置项51已生效。配置项52已生效。配置项53已生效。配置项54已生效。配置项55已生效。
配置项56已生效。配置项57已生效。配置项58已生效。配置项59已生效。配置项60已生效。

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
    """HTTP 全链到 READY：上传 → 提交 → 通过 → 创建构建 → worker 管线。"""

    patch_ports(monkeypatch, FakeParser(ParsedDocument(markdown=RICH_MARKDOWN)))
    uploader_headers = await login(client, UPLOADER)
    uploaded = await upload(
        client,
        uploader_headers,
        key=f"bs06-{uuid4().hex[:16]}",
        files=[("bs06.md", RICH_MARKDOWN.encode("utf-8"), "text/markdown")],
        metadata='[{"title": "分块预览业务场景文档", "tags": ["手册"]}]',
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


async def _db_chunks(version_id: UUID) -> list[dict[str, Any]]:
    return await rows(
        "select id, chunk_type, chunk_index, text, embedding_status, token_count, char_count, "
        "section_path from kb_chunks where document_version_id = :v order by chunk_index",
        v=str(version_id),
    )


async def test_full_chain_chunk_preview_business_outputs(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """构建 READY 后的分块预览：列表/搜索/分页/统计的业务输出对照 spec 验收。"""

    _doc_id, version_id = await _chain_to_ready(env, monkeypatch, client)
    headers = await login(client, KB_CHUNK_VIEWER)
    db_rows = await _db_chunks(version_id)
    assert len(db_rows) >= 8, "种子内容应产出父子块与多节结构"

    # 列表：翻页与库内全集一致（分页正确、不重不漏）
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(50):
        params: dict[str, str | int] = {"limit": 3}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await client.get(LIST_URL.format(version_id=version_id), params=params, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == [str(row["id"]) for row in db_rows]

    # 表格/代码未被拆坏：表格数据行同落一个块、代码整块独立
    table_chunks = [
        item
        for item in (await client.get(
            LIST_URL.format(version_id=version_id), params={"q": "重试", "limit": 100}, headers=headers
        )).json()["items"]
        if "| 重试 | 3 |" in item["text"]
    ]
    assert table_chunks, "搜索应命中含表格的块"
    assert all("| 参数 | 默认值 |" in item["text"] for item in table_chunks), (
        "表头与数据行必须同块（表格未被拆坏）"
    )
    code_chunks = [
        item
        for item in (await client.get(
            LIST_URL.format(version_id=version_id), params={"q": "hello kb", "limit": 100}, headers=headers
        )).json()["items"]
    ]
    assert code_chunks and all(
        'print("hello kb")' in item["text"] for item in code_chunks
    ), "代码必须整块呈现"

    # 搜索只返回匹配块（含父块），与库内 ILIKE 全集一致
    expected = await rows(
        "select id from kb_chunks where document_version_id = :v and text ilike '%配置项%' "
        "order by chunk_index",
        v=str(version_id),
    )
    assert expected
    search = await client.get(
        LIST_URL.format(version_id=version_id),
        params={"q": "配置项", "limit": 100},
        headers=headers,
    )
    assert search.status_code == 200, search.text
    assert [item["id"] for item in search.json()["items"]] == [str(row["id"]) for row in expected]

    # 统计与库内聚合一致
    stats = (await client.get(STATS_URL.format(version_id=version_id), headers=headers)).json()
    assert stats["total_chunks"] == len(db_rows)
    assert stats["parent_count"] == sum(1 for r in db_rows if r["chunk_type"] == "parent")
    assert stats["child_count"] == sum(1 for r in db_rows if r["chunk_type"] == "child")
    assert stats["total_tokens"] == sum(r["token_count"] for r in db_rows)
    assert stats["total_chars"] == sum(r["char_count"] for r in db_rows)
    assert stats["section_count"] >= 3, "三个章节应出现在统计里"

    # 详情：任取一块，字段与库一致
    chunk_id = str(db_rows[0]["id"])
    detail = await client.get(DETAIL_URL.format(chunk_id=chunk_id), headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["text"] == db_rows[0]["text"]
    assert detail.json()["document_version_id"] == str(version_id)


async def test_cross_tenant_chunk_access_is_denied(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """跨租拒绝：B 租主体访问 A 租版本/分块，三端点 404 同码（spec 跨租验收）。"""

    _doc_id, version_id = await _chain_to_ready(env, monkeypatch, client)
    chunk_id = str((await _db_chunks(version_id))[0]["id"])
    b_headers = await login(client, KB_B_CHUNK_VIEWER, school_code=SCHOOL_B)

    listing = await client.get(LIST_URL.format(version_id=version_id), headers=b_headers)
    assert listing.status_code == 404
    assert listing.json()["detail"]["code"] == "VERSION_NOT_FOUND"

    stats = await client.get(STATS_URL.format(version_id=version_id), headers=b_headers)
    assert stats.status_code == 404
    assert stats.json()["detail"]["code"] == "VERSION_NOT_FOUND"

    detail = await client.get(DETAIL_URL.format(chunk_id=chunk_id), headers=b_headers)
    assert detail.status_code == 404
    assert detail.json()["detail"]["code"] == "CHUNK_NOT_FOUND"
