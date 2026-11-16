"""M3 KB-06A 分块预览集成测试（真实 PG + 真实 Qdrant；解析/嵌入 fake ports）。

覆盖简报 `docs/design-briefs/M3-KB-06.md` §5 KB-06A 第 2–5 步的红→绿清单：
- 列表：keyset 分页不重不漏（含整页边界）、q 只返回匹配块（含父块）、LIKE 转义、
  section/chunk_type/parent_id 筛选、筛选+游标组合翻全集、版本定域不串版本；
- 详情：字段全集 + 父/子引用；
- 统计：与库内聚合逐字段对账；
- 404 链：不存在/跨租/self 范围外一律 404 且同码（不泄露存在性）；
- 403：缺 `school.kb.view_chunks`（`school.kb.view` 不能顶替）。

种子走 `seed_test_passed_version`（真实构建管线产出真实 kb_chunks 行，
被测对象是读链路而非构建——构建正确性由 KB-03A 用例背书）。
"""

import os
import uuid as uuid_module
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import (
    KB_B_CHUNK_VIEWER,
    KB_CHUNK_SELF,
    KB_CHUNK_VIEWER,
    NO_PERM,
    SCHOOL_B,
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

pytestmark = [pytest.mark.integration]

LIST_URL = "/api/v1/school/kb/versions/{version_id}/chunks"
STATS_URL = "/api/v1/school/kb/versions/{version_id}/chunks/stats"
DETAIL_URL = "/api/v1/school/kb/chunks/{chunk_id}"

RICH_MARKDOWN = """# 操作手册

## 第一章 安装

安装步骤1完成。安装步骤2完成。安装步骤3完成。安装步骤4完成。安装步骤5完成。
安装步骤6完成。安装步骤7完成。安装步骤8完成。安装步骤9完成。安装步骤10完成。

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
配置项61已生效。配置项62已生效。配置项63已生效。配置项64已生效。配置项65已生效。
配置项66已生效。配置项67已生效。配置项68已生效。配置项69已生效。配置项70已生效。
配置项71已生效。配置项72已生效。配置项73已生效。配置项74已生效。配置项75已生效。
配置项76已生效。配置项77已生效。配置项78已生效。配置项79已生效。配置项80已生效。

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
        monkeypatch, tmp_path, overrides={"QDRANT_URL": "http://127.0.0.1:56333"}
    ) as value:
        yield value


@pytest.fixture
async def client(env: KbEnv) -> AsyncIterator[AsyncClient]:
    async with make_client() as ac:
        yield ac


async def _seed_rich_version(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch
) -> tuple[UUID, UUID]:
    """构建一个多章节/表格/代码的 READY 版本，返回 (document_id, version_id)。"""

    return await _seed_test_passed_version(
        env, monkeypatch, title="分块预览用文档", markdown=RICH_MARKDOWN
    )


async def _chunk_viewer(client: AsyncClient) -> dict[str, str]:
    return await login(client, KB_CHUNK_VIEWER)


async def _db_chunks(version_id: UUID) -> list[dict[str, Any]]:
    """全量块行（管理员连接，绕过 RLS 的期望值通道），按 chunk_index 升序。"""

    return await rows(
        "select id, chunk_type, chunk_index, parent_id, text, text_markdown, section_path, "
        "page_start, page_end, token_count, char_count, embedding_status, chunk_hash "
        "from kb_chunks where document_version_id = :v order by chunk_index",
        v=str(version_id),
    )


# ---------------------------------------------------------------------------
# 列表：分页
# ---------------------------------------------------------------------------


async def test_list_chunks_walks_all_pages_without_gap_or_dup(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """limit=2 翻完全集：次序严格升序、无重复无遗漏、末页 next_cursor=None。"""

    _, version_id = await _seed_rich_version(env, monkeypatch)
    headers = await _chunk_viewer(client)
    expected_ids = [str(row["id"]) for row in await _db_chunks(version_id)]
    assert len(expected_ids) >= 6, "种子内容应产出足够块数（父子 + 多节）"

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(50):
        params: dict[str, str | int] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await client.get(LIST_URL.format(version_id=version_id), params=params, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == expected_ids, "游标翻页必须与库内全集按 chunk_index 一一对齐"


async def test_list_chunks_full_page_boundary_still_gives_cursor(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """「取满才给游标」边界：limit 恰好等于总数时给游标、下一页为空。"""

    _, version_id = await _seed_rich_version(env, monkeypatch)
    headers = await _chunk_viewer(client)
    total = len(await _db_chunks(version_id))

    first = await client.get(
        LIST_URL.format(version_id=version_id), params={"limit": total}, headers=headers
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert len(body["items"]) == total
    assert body["next_cursor"] is not None, "恰好整页也必须给游标，否则大文档静默截断"

    second = await client.get(
        LIST_URL.format(version_id=version_id),
        params={"limit": total, "cursor": body["next_cursor"]},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["items"] == []
    assert second.json()["next_cursor"] is None


# ---------------------------------------------------------------------------
# 列表：搜索与筛选（「只返回匹配块」）
# ---------------------------------------------------------------------------


async def test_list_chunks_search_returns_only_matching_blocks(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """q=配置项：只返回文本含该词的块（父块按拼接文本参与匹配），与库内 ILIKE 全集一致。"""

    _, version_id = await _seed_rich_version(env, monkeypatch)
    headers = await _chunk_viewer(client)
    expected = await rows(
        "select id, chunk_type, text from kb_chunks "
        "where document_version_id = :v and text ilike :pat order by chunk_index",
        v=str(version_id),
        pat="%配置项%",
    )
    assert expected, "种子内容应命中搜索词"
    assert any(row["chunk_type"] == "parent" for row in expected), (
        "父块（拼接文本）应参与匹配，否则表格/代码所在父块在搜索里不可见"
    )

    resp = await client.get(
        LIST_URL.format(version_id=version_id), params={"q": "配置项", "limit": 100}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [item["id"] for item in items] == [str(row["id"]) for row in expected]
    for item in items:
        assert "配置项" in item["text"]

    # 纯空白 q 规范化为「未提供」（审查 Minor 4）：端到端应返回全集
    total = len(await _db_chunks(version_id))
    blank = await client.get(
        LIST_URL.format(version_id=version_id), params={"q": "   ", "limit": 100}, headers=headers
    )
    assert blank.status_code == 200, blank.text
    assert len(blank.json()["items"]) == total


async def test_list_chunks_search_escapes_like_wildcards(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """q=% 必须按字面 % 搜索：未转义时等价于全匹配（假搜索）。"""

    _, version_id = await _seed_rich_version(env, monkeypatch)
    headers = await _chunk_viewer(client)
    total = len(await _db_chunks(version_id))

    resp = await client.get(
        LIST_URL.format(version_id=version_id), params={"q": "%", "limit": 100}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) < total, "通配符未转义会让 q=% 返回全部块"
    assert items == [item for item in items if "%" in item["text"]], "只允许返回含字面 % 的块"
    assert items == [], "种子内容不含字面 %，转义正确时应为空集"


async def test_list_chunks_section_filter_is_exact_match(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """section_path 精确匹配（简报卡 4：值来源 = stats sections 清单）。"""

    _, version_id = await _seed_rich_version(env, monkeypatch)
    headers = await _chunk_viewer(client)
    section = await rows(
        "select distinct section_path from kb_chunks "
        "where document_version_id = :v and section_path ilike '%第二章%'",
        v=str(version_id),
    )
    assert len(section) == 1, "种子结构应只有一个第二章 section_path"
    target = str(section[0]["section_path"])

    expected = await rows(
        "select id from kb_chunks where document_version_id = :v and section_path = :s "
        "order by chunk_index",
        v=str(version_id),
        s=target,
    )
    resp = await client.get(
        LIST_URL.format(version_id=version_id),
        params={"section_path": target, "limit": 100},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [item["id"] for item in items] == [str(row["id"]) for row in expected]
    assert all(item["section_path"] == target for item in items)


async def test_list_chunks_type_and_parent_filters(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """chunk_type 与 parent_id 筛选：只返回匹配块（parent_id 不含父块自身）。"""

    _, version_id = await _seed_rich_version(env, monkeypatch)
    headers = await _chunk_viewer(client)
    all_rows = await _db_chunks(version_id)

    resp = await client.get(
        LIST_URL.format(version_id=version_id),
        params={"chunk_type": "parent", "limit": 100},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    parent_items = resp.json()["items"]
    db_parents = [row for row in all_rows if row["chunk_type"] == "parent"]
    assert [item["id"] for item in parent_items] == [str(row["id"]) for row in db_parents]

    # 选一个子块数最多的父块，验证 parent_id 筛选只回子块
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for row in all_rows:
        if row["parent_id"]:
            by_parent.setdefault(str(row["parent_id"]), []).append(row)
    fat_parent, children = max(by_parent.items(), key=lambda kv: len(kv[1]))
    assert len(children) >= 2, "种子内容应产出至少一个多子块父块"

    resp = await client.get(
        LIST_URL.format(version_id=version_id),
        params={"parent_id": fat_parent, "limit": 100},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    child_items = resp.json()["items"]
    assert {item["id"] for item in child_items} == {str(row["id"]) for row in children}
    assert all(item["chunk_type"] == "child" for item in child_items)
    assert all(item["parent_id"] == fat_parent for item in child_items)


async def test_list_chunks_filtered_pagination_is_complete(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """筛选 + limit=1 翻全集：与库内筛选结果不重不漏（游标推进在过滤之后）。"""

    _, version_id = await _seed_rich_version(env, monkeypatch)
    headers = await _chunk_viewer(client)
    expected = await rows(
        "select id from kb_chunks where document_version_id = :v and text ilike '%配置项%' "
        "order by chunk_index",
        v=str(version_id),
    )

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(50):
        params: dict[str, str | int] = {"q": "配置项", "limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await client.get(LIST_URL.format(version_id=version_id), params=params, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == [str(row["id"]) for row in expected]


async def test_list_chunks_is_scoped_to_requested_version(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """同一文档两个版本：v1 的列表只含 v1 的块，不混入 v2（spec 版本定域验收）。"""

    doc_id, v1 = await _seed_rich_version(env, monkeypatch)
    _, v2 = await _seed_test_passed_version(
        env, monkeypatch, doc_id=doc_id, version_no=2, title="分块预览用文档",
        markdown=RICH_MARKDOWN,
    )
    headers = await _chunk_viewer(client)
    v1_rows = await _db_chunks(v1)
    v2_rows = await _db_chunks(v2)
    assert {str(r["id"]) for r in v1_rows}.isdisjoint({str(r["id"]) for r in v2_rows})

    resp = await client.get(LIST_URL.format(version_id=v1), params={"limit": 100}, headers=headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [item["id"] for item in items] == [str(row["id"]) for row in v1_rows]
    assert str(v2) not in {item["id"] for item in items}


# ---------------------------------------------------------------------------
# 列表：404 / 400 / 422
# ---------------------------------------------------------------------------


async def test_list_chunks_404_chain_and_invalid_cursor(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """不存在/跨租/self 范围外 → 404 同码；垃圾游标 → 400 INVALID_CURSOR。"""

    _, version_id = await _seed_rich_version(env, monkeypatch)
    headers = await _chunk_viewer(client)
    self_headers = await login(client, KB_CHUNK_SELF)
    b_headers = await login(client, KB_B_CHUNK_VIEWER, school_code=SCHOOL_B)

    missing = await client.get(LIST_URL.format(version_id=uuid4()), headers=headers)
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "VERSION_NOT_FOUND"

    cross = await client.get(LIST_URL.format(version_id=version_id), headers=b_headers)
    assert cross.status_code == 404
    assert cross.json()["detail"]["code"] == "VERSION_NOT_FOUND"

    invisible = await client.get(LIST_URL.format(version_id=version_id), headers=self_headers)
    assert invisible.status_code == 404
    assert invisible.json()["detail"]["code"] == "VERSION_NOT_FOUND"

    bad_cursor = await client.get(
        LIST_URL.format(version_id=version_id),
        params={"cursor": "garbage-cursor"},
        headers=headers,
    )
    assert bad_cursor.status_code == 400
    assert bad_cursor.json()["detail"]["code"] == "INVALID_CURSOR"

    bad_type = await client.get(
        LIST_URL.format(version_id=version_id), params={"chunk_type": "root"}, headers=headers
    )
    assert bad_type.status_code == 422

    # limit 路由边界（审查 Minor 4）：0 与 101 都 422
    for bad_limit in ("0", "101"):
        resp = await client.get(
            LIST_URL.format(version_id=version_id), params={"limit": bad_limit}, headers=headers
        )
        assert resp.status_code == 422, f"limit={bad_limit} 应 422"


async def test_list_chunks_rejects_overlong_q(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """q 超过 200 字符 → 422（不进 ILIKE，避免无界模式串）。"""

    _, version_id = await _seed_rich_version(env, monkeypatch)
    headers = await _chunk_viewer(client)
    resp = await client.get(
        LIST_URL.format(version_id=version_id), params={"q": "字" * 201}, headers=headers
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 详情
# ---------------------------------------------------------------------------


async def test_get_chunk_detail_returns_fields_and_family(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """详情字段全集 + 父/子引用：父块回 children（按 chunk_index 升序），子块回 parent。"""

    _, version_id = await _seed_rich_version(env, monkeypatch)
    headers = await _chunk_viewer(client)
    all_rows = await _db_chunks(version_id)
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for row in all_rows:
        if row["parent_id"]:
            by_parent.setdefault(str(row["parent_id"]), []).append(row)
    fat_parent, children = max(by_parent.items(), key=lambda kv: len(kv[1]))
    parent_row = next(row for row in all_rows if str(row["id"]) == fat_parent)
    child_row = children[0]

    resp = await client.get(DETAIL_URL.format(chunk_id=fat_parent), headers=headers)
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["id"] == fat_parent
    assert detail["chunk_type"] == "parent"
    assert detail["chunk_index"] == parent_row["chunk_index"]
    assert detail["document_version_id"] == str(version_id)
    assert detail["text"] == parent_row["text"]
    assert detail["text_markdown"] == parent_row["text_markdown"]
    assert detail["section_path"] == parent_row["section_path"]
    assert detail["token_count"] == parent_row["token_count"]
    assert detail["char_count"] == parent_row["char_count"]
    assert detail["embedding_status"] == parent_row["embedding_status"]
    assert detail["parent"] is None
    assert [c["id"] for c in detail["children"]] == [str(row["id"]) for row in children]
    assert [c["chunk_index"] for c in detail["children"]] == sorted(
        c["chunk_index"] for c in detail["children"]
    )

    resp = await client.get(DETAIL_URL.format(chunk_id=str(child_row["id"])), headers=headers)
    assert resp.status_code == 200, resp.text
    child_detail = resp.json()
    assert child_detail["parent"] == {"id": fat_parent, "chunk_index": parent_row["chunk_index"]}
    assert child_detail["children"] == []


async def test_chunk_hash_exposed_for_edit_base_hash(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """KB-10B（卡 E2）：列表/详情回包透出 chunk_hash——前端 base_hash 供给唯一来源
    （值与库内一致；与编辑端点乐观锁判据同源，前端不复刻 hash 算法）。"""

    _, version_id = await _seed_rich_version(env, monkeypatch)
    headers = await _chunk_viewer(client)
    rows = await _db_chunks(version_id)
    child = next(row for row in rows if row["chunk_type"] == "child")

    resp = await client.get(LIST_URL.format(version_id=version_id), headers=headers)
    assert resp.status_code == 200, resp.text
    by_id = {item["id"]: item for item in resp.json()["items"]}
    assert by_id[str(child["id"])]["chunk_hash"] == child["chunk_hash"]

    detail = await client.get(DETAIL_URL.format(chunk_id=str(child["id"])), headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["chunk_hash"] == child["chunk_hash"]


async def test_get_chunk_404_chain_does_not_leak_existence(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """详情 404 三态（不存在/跨租/self 范围外）同码同文——不区分「不存在」与「不可见」。"""

    _, version_id = await _seed_rich_version(env, monkeypatch)
    headers = await _chunk_viewer(client)
    self_headers = await login(client, KB_CHUNK_SELF)
    b_headers = await login(client, KB_B_CHUNK_VIEWER, school_code=SCHOOL_B)
    chunk_rows = await _db_chunks(version_id)
    chunk_id = str(chunk_rows[0]["id"])

    codes: list[str] = []
    messages: list[str] = []
    for probe_headers, probe_id in (
        (headers, str(uuid4())),
        (b_headers, chunk_id),
        (self_headers, chunk_id),
    ):
        resp = await client.get(DETAIL_URL.format(chunk_id=probe_id), headers=probe_headers)
        assert resp.status_code == 404, resp.text
        codes.append(resp.json()["detail"]["code"])
        messages.append(resp.json()["detail"]["message"])
    assert set(codes) == {"CHUNK_NOT_FOUND"}
    assert set(messages) == {messages[0]}, "三态必须同一文案，否则泄露存在性"


async def test_get_chunk_of_other_version_same_document_is_visible(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """详情按块自身的版本定域：同租可见版本的块可单查（不要求来自当前版本上下文）。"""

    doc_id, v1 = await _seed_rich_version(env, monkeypatch)
    _, _v2 = await _seed_test_passed_version(
        env, monkeypatch, doc_id=doc_id, version_no=2, title="分块预览用文档",
        markdown=RICH_MARKDOWN,
    )
    headers = await _chunk_viewer(client)
    chunk_id = str((await _db_chunks(v1))[0]["id"])
    resp = await client.get(DETAIL_URL.format(chunk_id=chunk_id), headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["document_version_id"] == str(v1)


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


async def test_stats_matches_seeded_set(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """统计与库内聚合逐字段对账：总数/父子/embedding 三态/token/字符/章节清单。"""

    _, version_id = await _seed_rich_version(env, monkeypatch)
    headers = await _chunk_viewer(client)
    all_rows = await _db_chunks(version_id)

    resp = await client.get(STATS_URL.format(version_id=version_id), headers=headers)
    assert resp.status_code == 200, resp.text
    stats = resp.json()

    assert stats["total_chunks"] == len(all_rows)
    assert stats["parent_count"] == sum(1 for r in all_rows if r["chunk_type"] == "parent")
    assert stats["child_count"] == sum(1 for r in all_rows if r["chunk_type"] == "child")
    for status in ("pending", "ready", "failed"):
        assert stats["embedding_status"][status] == sum(
            1 for r in all_rows if r["embedding_status"] == status
        )
    assert stats["total_tokens"] == sum(r["token_count"] for r in all_rows)
    assert stats["total_chars"] == sum(r["char_count"] for r in all_rows)

    section_counts: dict[str, int] = {}
    for row in all_rows:
        if row["section_path"] is not None:
            section_counts[str(row["section_path"])] = (
                section_counts.get(str(row["section_path"]), 0) + 1
            )
    assert stats["section_count"] == len(section_counts)
    expected_sections = sorted(
        section_counts.items(), key=lambda kv: (-kv[1], kv[0])
    )
    assert [(s["section_path"], s["chunk_count"]) for s in stats["sections"]] == expected_sections


async def test_stats_is_not_affected_by_other_version_or_filters(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """统计是 per-version 固定口径（简报 §3 显式化）：不接筛选参数、不串版本。"""

    doc_id, v1 = await _seed_rich_version(env, monkeypatch)
    _, v2 = await _seed_test_passed_version(
        env, monkeypatch, doc_id=doc_id, version_no=2, title="分块预览用文档",
        markdown=RICH_MARKDOWN,
    )
    headers = await _chunk_viewer(client)
    v1_total = len(await _db_chunks(v1))
    v2_total = len(await _db_chunks(v2))

    first = await client.get(STATS_URL.format(version_id=v1), headers=headers)
    second = await client.get(STATS_URL.format(version_id=v2), headers=headers)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["total_chunks"] == v1_total
    assert second.json()["total_chunks"] == v2_total


async def test_stats_404_chain(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """统计端点复用同一 404 链：不存在/跨租/self 范围外。"""

    _, version_id = await _seed_rich_version(env, monkeypatch)
    headers = await _chunk_viewer(client)
    self_headers = await login(client, KB_CHUNK_SELF)
    b_headers = await login(client, KB_B_CHUNK_VIEWER, school_code=SCHOOL_B)

    for probe_headers, probe_version in (
        (headers, uuid4()),
        (b_headers, version_id),
        (self_headers, version_id),
    ):
        resp = await client.get(STATS_URL.format(version_id=probe_version), headers=probe_headers)
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["code"] == "VERSION_NOT_FOUND"


# ---------------------------------------------------------------------------
# 权限与数据范围
# ---------------------------------------------------------------------------


async def test_chunks_endpoints_require_view_chunks_permission(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """三个端点都只认 school.kb.view_chunks：无码 403，school.kb.view 不能顶替。"""

    _, version_id = await _seed_rich_version(env, monkeypatch)
    chunk_rows = await _db_chunks(version_id)
    chunk_id = str(chunk_rows[0]["id"])
    no_perm_headers = await login(client, NO_PERM)
    view_only_headers = await login(client, UPLOADER)  # upload+view，无 view_chunks

    for headers in (no_perm_headers, view_only_headers):
        for url, fmt in ((LIST_URL, version_id), (STATS_URL, version_id), (DETAIL_URL, chunk_id)):
            resp = await client.get(url.format(version_id=fmt, chunk_id=chunk_id), headers=headers)
            assert resp.status_code == 403, f"{url} 应对缺码主体 403：{resp.text}"


async def test_self_scope_viewer_sees_only_owned_document_chunks(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """self 范围正反向：自己的文档分块可见，他人的文档 404；school 范围全可见。"""

    doc_owned, version_owned = await _seed_rich_version(env, monkeypatch)
    _doc_other, version_other = await _seed_test_passed_version(
        env, monkeypatch, title="他人文档", markdown=RICH_MARKDOWN
    )
    self_user = await rows(
        "select id from users where login_id = :lid", lid=KB_CHUNK_SELF
    )
    self_uid = str(self_user[0]["id"])
    await execute(
        "update kb_documents set created_by = :uid where id = :doc",
        uid=self_uid,
        doc=str(doc_owned),
    )
    await execute(
        "update kb_document_versions set created_by = :uid where document_id = :doc",
        uid=self_uid,
        doc=str(doc_owned),
    )

    self_headers = await login(client, KB_CHUNK_SELF)
    school_headers = await _chunk_viewer(client)

    own = await client.get(
        LIST_URL.format(version_id=version_owned), params={"limit": 100}, headers=self_headers
    )
    assert own.status_code == 200, own.text
    assert own.json()["items"], "self 范围应看到自己文档的全部分块"

    other = await client.get(LIST_URL.format(version_id=version_other), headers=self_headers)
    assert other.status_code == 404
    assert other.json()["detail"]["code"] == "VERSION_NOT_FOUND"

    school_view = await client.get(
        LIST_URL.format(version_id=version_other), params={"limit": 100}, headers=school_headers
    )
    assert school_view.status_code == 200, school_view.text
    assert school_view.json()["items"]
    assert uuid_module.UUID(str(school_view.json()["items"][0]["id"]))
